from __future__ import annotations

import io
import json
import re
import sqlite3
import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib import error as urllib_error
from urllib.parse import parse_qs

from dereel.db import Database
from dereel.media import add_text_overlay, atomic_write_stream, sha256_file, text_overlay_backend, validate_audio, validate_video
from dereel.pipeline import add_project_asset, asset_view, audit_asset_health, bulk_review_assets, bulk_review_content_assets, check_asset_health, cleanup_pending_asset, create_asset, create_job, create_project, delete_asset, delete_project, get_project_assets, get_project_audio, list_asset_reviews, list_assets, list_content_reviews, move_project_asset, plan_project, remove_project_asset, render_project, retry_failed_job, review_asset, review_content_asset, score_asset, score_breakdown, set_job, update_project, update_project_asset, validate_project_output, validate_render_outputs
from dereel.scout import _host_allowed, _request_json, import_provider_asset, import_provider_assets
from dereel.automation import claim_automation, create_automation, create_automation_run, due_automations, finish_automation, finish_automation_run, list_automation_runs, run_automation, scan_provider_sources, scan_watch_folder, update_automation
from dereel.publisher import account_view, backfill_publish_integrity, begin_publish_attempt, cancel_publish_job, check_publishing_account, claim_publish_job, complete_publish_attempt, complete_publish_job, create_publish_jobs, delete_publishing_account, fail_publish_attempt, force_publish_success, list_publish_attempts, mark_publish_job, platform_capabilities, publish_instagram_reel, publish_job, publish_job_view, publish_provenance, publish_result_status, publish_tiktok_draft, publish_youtube, reschedule_publish_job, update_publishing_account, validate_export
from dereel.config import Settings
from dereel.server import AppState, Handler, close_multipart_files, dashboard_counts, finalize_publish_success, parse_multipart, parse_multipart_stream, recover_interrupted_jobs, runtime_diagnostics, safe_path


class CoreModelTests(unittest.TestCase):
    def test_atomic_stream_write_preserves_final_file_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "clip.mp4"
            destination.write_bytes(b"previous")

            class FailingStream:
                def __init__(self) -> None:
                    self.calls = 0

                def read(self, _size: int) -> bytes:
                    self.calls += 1
                    if self.calls == 1:
                        return b"partial"
                    raise OSError("source interrupted")

            with self.assertRaisesRegex(OSError, "source interrupted"):
                atomic_write_stream(FailingStream(), destination)

            self.assertEqual(destination.read_bytes(), b"previous")
            self.assertEqual(list(root.glob("*.part")), [])

    def test_local_file_serving_supports_streaming_ranges_and_path_guards(self) -> None:
        class FakeHandler:
            serve_file = Handler.serve_file

            def __init__(self, range_header: str = "") -> None:
                from email.message import Message

                self.headers = Message()
                if range_header:
                    self.headers["Range"] = range_header
                self.status = None
                self.response_headers = {}
                self.body = bytearray()
                self.wfile = self

            def send_response(self, status: int) -> None:
                self.status = status

            def send_header(self, name: str, value: str) -> None:
                self.response_headers[name] = value

            def end_headers(self) -> None:
                return

            def write(self, data: bytes) -> int:
                self.body.extend(data)
                return len(data)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "preview.mp4"
            media.write_bytes(b"0123456789")

            full = FakeHandler()
            full.serve_file(media)
            self.assertEqual((full.status, bytes(full.body)), (200, b"0123456789"))
            self.assertEqual(full.response_headers["Content-Length"], "10")
            self.assertEqual(full.response_headers["Accept-Ranges"], "bytes")

            empty = root / "empty.txt"
            empty.touch()
            empty_response = FakeHandler()
            empty_response.serve_file(empty)
            self.assertEqual((empty_response.status, bytes(empty_response.body)), (200, b""))
            self.assertEqual(empty_response.response_headers["Content-Length"], "0")

            partial = FakeHandler("bytes=2-5")
            partial.serve_file(media)
            self.assertEqual((partial.status, bytes(partial.body)), (206, b"2345"))
            self.assertEqual(partial.response_headers["Content-Range"], "bytes 2-5/10")

            suffix = FakeHandler("bytes=-3")
            suffix.serve_file(media)
            self.assertEqual((suffix.status, bytes(suffix.body)), (206, b"789"))

            invalid = FakeHandler("bytes=8-2")
            invalid.serve_file(media)
            self.assertEqual(invalid.status, 416)
            self.assertEqual(invalid.response_headers["Content-Range"], "bytes */10")
            self.assertEqual(bytes(invalid.body), b"")

            with self.assertRaisesRegex(ValueError, "outside"):
                safe_path(root, "../outside.mp4")

    def test_multipart_parser_preserves_binary_file_trailing_bytes(self) -> None:
        boundary = "DeReelBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="clip.mp4"\r\n'
            "Content-Type: video/mp4\r\n\r\n"
        ).encode() + b"binary-data\r\n" + f"\r\n--{boundary}--\r\n".encode()
        fields, files = parse_multipart(f"multipart/form-data; boundary={boundary}", body)
        self.assertEqual(fields, {})
        self.assertEqual(files["file"], ("clip.mp4", b"binary-data\r\n", "video/mp4"))

    def test_streaming_multipart_parser_handles_small_reads(self) -> None:
        import io

        class ChunkedStream(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                return super().read(min(size, 7) if size >= 0 else 7)

        boundary = "ChunkBoundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n"
            "A tiny upload\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.mp4\"\r\n"
            "Content-Type: video/mp4\r\n\r\n"
        ).encode() + b"0123456789\r\n-" + f"\r\n--{boundary}--\r\n".encode()
        fields, files = parse_multipart_stream(
            f"multipart/form-data; boundary={boundary}", ChunkedStream(body), len(body)
        )
        try:
            self.assertEqual(fields, {"title": "A tiny upload"})
            self.assertEqual(files["file"][1].read(), b"0123456789\r\n-")
        finally:
            close_multipart_files(files)

    def test_database_initializes_and_preserves_asset_rights(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            asset = create_asset(
                db,
                kind="reference",
                title="A funny cat",
                source_url="https://example.com/video",
                platform="YouTube",
                tags=["Cats", "funny"],
            )
            self.assertEqual(asset["rights_status"], "private_reference")
            self.assertEqual(asset["tags"], ["cats", "funny"])
            with db.connection() as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
                publish_columns = {row[1] for row in connection.execute("PRAGMA table_info(publish_jobs)").fetchall()}
                attempt_columns = {row[1] for row in connection.execute("PRAGMA table_info(publish_attempts)").fetchall()}
                publish_schema = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'publish_jobs'").fetchone()[0]
                attempt_schema = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'publish_attempts'").fetchone()[0]
                review_columns = {row[1] for row in connection.execute("PRAGMA table_info(asset_reviews)").fetchall()}
                content_review_columns = {row[1] for row in connection.execute("PRAGMA table_info(asset_content_reviews)").fetchall()}
                content_review_indexes = {row[1] for row in connection.execute("PRAGMA index_list(asset_content_reviews)").fetchall()}
                automation_columns = {row[1] for row in connection.execute("PRAGMA table_info(automations)").fetchall()}
                schema_version = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
            self.assertIn("automation_id", columns)
            self.assertIn("retry_of", columns)
            self.assertIn("attempt", columns)
            self.assertIn("claim_token", publish_columns)
            self.assertIn("claimed_at", publish_columns)
            self.assertIn("'drafted'", publish_schema)
            self.assertIn("'drafted'", attempt_schema)
            self.assertEqual(schema_version, "13")
            self.assertIn("account_ids_json", automation_columns)
            self.assertIn("matches_json", content_review_columns)
            self.assertIn("idx_asset_content_reviews_asset", content_review_indexes)
            self.assertIn("attempt_number", attempt_columns)
            self.assertIn("cancelled_at", publish_columns)
            self.assertIn("previous_rights_status", review_columns)

    def test_database_migrates_legacy_publish_status_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy-publish.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', '10');
                CREATE TABLE projects (id TEXT PRIMARY KEY);
                INSERT INTO projects(id) VALUES ('project_legacy');
                CREATE TABLE publishing_accounts (id TEXT PRIMARY KEY);
                CREATE TABLE publish_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    account_id TEXT,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'ready', 'published', 'failed')),
                    scheduled_for TEXT,
                    remote_id TEXT,
                    error TEXT,
                    claim_token TEXT,
                    claimed_at TEXT,
                    cancelled_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE publish_attempts (
                    id TEXT PRIMARY KEY,
                    publish_job_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    trigger TEXT NOT NULL CHECK(trigger IN ('manual', 'scheduler')),
                    status TEXT NOT NULL CHECK(status IN ('running', 'published', 'failed')),
                    remote_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(publish_job_id, attempt_number)
                );
                INSERT INTO publish_jobs(id, project_id, platform, status, payload_json, created_at, updated_at) VALUES ('legacy_job', 'project_legacy', 'tiktok', 'published', '{}', '2026-01-01', '2026-01-01');
                INSERT INTO publish_attempts(id, publish_job_id, attempt_number, trigger, status, started_at, updated_at) VALUES ('legacy_attempt', 'legacy_job', 1, 'manual', 'published', '2026-01-01', '2026-01-01');
                """
            )
            connection.commit()
            connection.close()

            db = Database(database_path)
            with db.connection() as migrated:
                migrated.execute(
                    "INSERT INTO publish_jobs(id, project_id, platform, status, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, '{}', ?, ?)",
                    ("draft_job", "project_legacy", "tiktok", "drafted", "2026-01-02", "2026-01-02"),
                )
                migrated.execute(
                    "INSERT INTO publish_attempts(id, publish_job_id, attempt_number, trigger, status, started_at, updated_at) VALUES (?, ?, 1, 'manual', 'drafted', ?, ?)",
                    ("draft_attempt", "draft_job", "2026-01-02", "2026-01-02"),
                )
                self.assertEqual(migrated.execute("SELECT status FROM publish_jobs WHERE id = 'legacy_job'").fetchone()[0], "published")
                self.assertEqual(migrated.execute("SELECT status FROM publish_attempts WHERE id = 'legacy_attempt'").fetchone()[0], "published")

    def test_database_migrates_legacy_jobs_with_automation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3');
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.commit()
            connection.close()

            db = Database(database_path)
            with db.connection() as migrated:
                columns = {row[1] for row in migrated.execute("PRAGMA table_info(jobs)").fetchall()}
                schema_version = migrated.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
                index_names = {row[1] for row in migrated.execute("PRAGMA index_list(jobs)").fetchall()}
            self.assertIn("automation_id", columns)
            self.assertIn("retry_of", columns)
            self.assertIn("attempt", columns)
            self.assertIn("idx_jobs_automation", index_names)
            self.assertIn("idx_jobs_retry_of", index_names)
            self.assertEqual(schema_version, "13")

    def test_media_fingerprints_prevent_duplicate_library_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            create_asset(db, kind="media", title="First clip", local_path="/tmp/first.mp4", metadata={"fingerprint": "same"}, rights_status="pending_review")
            with self.assertRaisesRegex(ValueError, "matches existing asset"):
                create_asset(db, kind="media", title="Repeated clip", local_path="/tmp/second.mp4", metadata={"fingerprint": "same"}, rights_status="pending_review")

    def test_review_flags_keep_provider_and_local_attestations_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            provider = create_asset(db, kind="media", title="Stock clip", platform="pexels", local_path="/tmp/stock.mp4", rights_status="pending_review")
            local = create_asset(db, kind="media", title="Owned clip", platform="local", local_path="/tmp/owned.mp4", rights_status="pending_review", metadata={"fingerprint": "owned"})
            self.assertEqual(provider["review_flags"], ["provider-license-review"])
            self.assertEqual(local["review_flags"], ["rights-attestation"])

    def test_asset_provenance_distinguishes_reference_provider_and_automation_origins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            reference = create_asset(db, kind="reference", title="Saved link", platform="TikTok", source_url="https://example.com/reference")
            provider = create_asset(db, kind="media", title="Stock clip", platform="pexels", source_url="https://pexels.com/video/1", local_path="/tmp/stock.mp4", rights_status="pending_review", metadata={"fingerprint": "stock"})
            automation = create_asset(db, kind="media", title="Watched clip", platform="local-watch", source_url="file:///tmp/watched.mp4", local_path="/tmp/watched.mp4", rights_status="owned_distribution", automation_id="automation_1", metadata={"fingerprint": "watched"})
            self.assertEqual(reference["provenance"]["source_type"], "reference")
            self.assertEqual(provider["provenance"]["source_type"], "provider_import")
            self.assertEqual(automation["provenance"]["source_type"], "automation_import")
            self.assertEqual(automation["provenance"]["fingerprint"], "watched")

    def test_bulk_review_updates_rights_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            first = create_asset(db, kind="media", title="First", local_path="/tmp/first.mp4", rights_status="pending_review")
            second = create_asset(db, kind="media", title="Second", local_path="/tmp/second.mp4", rights_status="pending_review")
            approved = bulk_review_assets(db, [first["id"], second["id"]], "approve_licensed")
            self.assertEqual([asset["rights_status"] for asset in approved], ["licensed_distribution", "licensed_distribution"])
            self.assertEqual(len(list_asset_reviews(db, first["id"])), 1)
            self.assertEqual(list_asset_reviews(db, first["id"])[0]["action"], "approve_licensed")
            rejected = create_asset(db, kind="media", title="Rejected", local_path="/tmp/rejected.mp4", rights_status="pending_review")
            self.assertEqual(bulk_review_assets(db, [rejected["id"]], "reject")[0]["rights_status"], "blocked")
            pending = create_asset(db, kind="media", title="Pending", local_path="/tmp/pending.mp4", rights_status="pending_review")
            reference = create_asset(db, kind="reference", title="Reference", source_url="https://example.com")
            with self.assertRaisesRegex(ValueError, "awaiting review"):
                bulk_review_assets(db, [pending["id"], reference["id"]], "approve_owned")
            self.assertEqual(db.get("assets", "id", pending["id"])["rights_status"], "pending_review")

    def test_single_review_records_transition_and_allows_rejection_of_cleared_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            asset = create_asset(db, kind="media", title="Owned clip", local_path="/tmp/owned.mp4", rights_status="pending_review")
            reviewed = review_asset(db, asset["id"], "approve_owned", "I own this recording")
            self.assertEqual(reviewed["rights_status"], "owned_distribution")
            self.assertEqual(reviewed["status"], "approved")
            history = list_asset_reviews(db, asset["id"])
            self.assertEqual(history[0]["previous_rights_status"], "pending_review")
            self.assertEqual(history[0]["next_rights_status"], "owned_distribution")
            self.assertEqual(history[0]["note"], "I own this recording")
            blocked = review_asset(db, asset["id"], "reject")
            self.assertEqual(blocked["rights_status"], "blocked")
            self.assertEqual([item["action"] for item in list_asset_reviews(db, asset["id"])], ["reject", "approve_owned"])

    def test_asset_health_detects_missing_changed_invalid_and_recovered_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            missing = create_asset(db, kind="media", title="Missing", local_path=str(root / "gone.mp4"), rights_status="pending_review", metadata={"duration": 2, "width": 720, "height": 1280})
            missing_checked = check_asset_health(db, missing["id"])
            self.assertEqual(missing_checked["health"]["status"], "missing")
            self.assertIn("missing-local-file", missing_checked["review_flags"])
            baseline_path = root / "baseline.mp4"
            baseline_path.write_bytes(b"baseline")
            baseline = create_asset(db, kind="media", title="Baseline", local_path=str(baseline_path), rights_status="pending_review", metadata={"duration": 2, "width": 720, "height": 1280})
            with patch("dereel.pipeline.validate_video", return_value={"duration": 2, "file_size": 8, "width": 720, "height": 1280, "has_audio": False}):
                baseline_checked = check_asset_health(db, baseline["id"])
            self.assertEqual(db.get("assets", "id", baseline["id"])["fingerprint"], baseline_checked["health"]["fingerprint"])
            baseline_path.unlink()
            refreshed = asset_view(db.get("assets", "id", baseline["id"]) or {})
            self.assertEqual(refreshed["health"]["status"], "missing")
            changed_path = root / "changed.mp4"
            changed_path.write_bytes(b"changed")
            changed = create_asset(db, kind="media", title="Changed", local_path=str(changed_path), rights_status="pending_review", metadata={"duration": 2, "width": 720, "height": 1280, "fingerprint": "before"})
            with patch("dereel.pipeline.validate_video", return_value={"duration": 2, "file_size": 7, "width": 720, "height": 1280, "has_audio": False}):
                changed_checked = check_asset_health(db, changed["id"])
            self.assertEqual(changed_checked["health"]["status"], "changed")
            with patch("dereel.pipeline.validate_video", side_effect=ValueError("corrupt container")):
                invalid = check_asset_health(db, changed["id"])
            self.assertEqual(invalid["health"]["status"], "invalid")
            report = audit_asset_health(db, [missing["id"]])
            self.assertEqual((report["checked"], report["missing"]), (1, 1))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_automation_imports_reviews_renders_and_deduplicates_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watch_dir = root / "watch"
            watch_dir.mkdir()
            source = watch_dir / "cat.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=0x31565a:s=320x568:d=1.2",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
                ],
                check=True,
            )
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Cats", "watch_dir": str(watch_dir), "theme": "cat funny", "default_rights": "pending_review"})
            first = run_automation(db, automation["id"], root / "library", root / "outputs")
            self.assertEqual(first["status"], "awaiting_review")
            finish_automation(db, automation["id"], first)
            with db.connection() as connection:
                asset = connection.execute("SELECT id FROM assets WHERE automation_id = ?", (automation["id"],)).fetchone()
                self.assertIsNotNone(asset)
                connection.execute("UPDATE assets SET rights_status = 'owned_distribution', status = 'approved' WHERE id = ?", (asset["id"],))
            second = run_automation(db, automation["id"], root / "library", root / "outputs")
            self.assertEqual(second["status"], "rendered")
            self.assertTrue((Path(second["output_dir"]) / "final.mp4").is_file())
            finish_automation(db, automation["id"], second)
            third = run_automation(db, automation["id"], root / "library", root / "outputs")
            self.assertEqual(third["status"], "idle")
            (Path(second["output_dir"]) / "final.mp4").unlink()
            fourth = run_automation(db, automation["id"], root / "library", root / "outputs")
            self.assertEqual(fourth["status"], "rendered")
            with db.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

    def test_automation_claim_prevents_duplicate_scheduler_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            automation = create_automation(db, {"name": "Scheduled", "watch_dir": directory, "enabled": True, "interval_minutes": 15})
            self.assertEqual([item["id"] for item in due_automations(db)], [automation["id"]])
            self.assertTrue(claim_automation(db, automation["id"]))
            self.assertEqual(due_automations(db), [])

    def test_watch_duplicate_race_is_reported_as_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watch_dir = root / "watch"
            watch_dir.mkdir()
            source = watch_dir / "cat.mp4"
            source.write_bytes(b"video")
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Cats", "watch_dir": str(watch_dir)})
            metadata = {"duration": 2, "file_size": source.stat().st_size, "width": 720, "height": 1280, "has_audio": False}
            with patch("dereel.automation.validate_video", return_value=metadata), patch("dereel.automation.sha256_file", return_value="same"), patch("dereel.automation.create_asset", side_effect=ValueError("Media matches existing asset 'Existing' (asset_1)")):
                result = scan_watch_folder(db, automation, root / "library")
            self.assertEqual(result["duplicates"], [source.name])
            self.assertEqual(result["errors"], [])
            self.assertEqual(list((root / "library").iterdir()), [])

    def test_scheduler_recovers_after_transient_database_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = AppState(
                Settings(
                    root=root,
                    data_dir=root / "data",
                    database_path=root / "data" / "test.sqlite3",
                    originals_dir=root / "library",
                    audio_dir=root / "audio",
                    output_dir=root / "outputs",
                    web_dir=root,
                )
            )
            with patch.object(
                app,
                "queue_due_automations",
                side_effect=[sqlite3.OperationalError("unable to open database file"), None],
            ), patch.object(app.stop_event, "wait", side_effect=[False, False, True]):
                app.scheduler_loop()
            status = app.scheduler_diagnostics()
            self.assertEqual(status["status"], "ok")
            self.assertEqual(status["last_error"], "unable to open database file")
            self.assertIsNotNone(status["last_success_at"])

    def test_active_automation_freezes_configuration_but_allows_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            automation = create_automation(db, {"name": "Active recipe", "watch_dir": directory})
            self.assertTrue(claim_automation(db, automation["id"]))
            with self.assertRaisesRegex(ValueError, "cannot change while it is processing"):
                update_automation(db, automation["id"], {"name": "Changed while active"})
            paused = update_automation(db, automation["id"], {"enabled": False})
            self.assertFalse(paused["enabled"])

    def test_automation_configuration_edit_rebuilds_next_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            automation = create_automation(db, {"name": "Recipe", "watch_dir": directory, "theme": "old theme"})
            project = create_project(db, {"name": "Old run", "theme": "old theme"})
            db.update("automations", "id", automation["id"], {"last_project_id": project["id"]})
            unchanged = update_automation(db, automation["id"], {"enabled": True})
            self.assertEqual(unchanged["last_project_id"], project["id"])
            destination_edit = update_automation(db, automation["id"], {"publish_delay_minutes": 15, "platforms": ["youtube"]})
            self.assertEqual(destination_edit["last_project_id"], project["id"])
            changed = update_automation(db, automation["id"], {"theme": "new theme"})
            self.assertEqual(changed["last_project_id"], "")

    def test_owned_watch_import_records_automation_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watch_dir = root / "watch"
            watch_dir.mkdir()
            (watch_dir / "owned.mp4").write_bytes(b"video")
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Owned watch", "watch_dir": str(watch_dir), "default_rights": "owned_distribution"})
            metadata = {"duration": 2, "file_size": 5, "width": 720, "height": 1280, "has_audio": False}
            with patch("dereel.automation.validate_video", return_value=metadata), patch("dereel.automation.sha256_file", return_value="owned-fingerprint"):
                result = scan_watch_folder(db, automation, root / "library")
            asset = result["imported"][0]
            self.assertEqual((asset["rights_status"], asset["status"]), ("owned_distribution", "approved"))
            history = list_asset_reviews(db, asset["id"])
            self.assertEqual((history[0]["action"], history[0]["previous_rights_status"], history[0]["note"]), ("approve_owned", "pending_review", "Automation attestation: owned distribution"))

    def test_production_gate_rejects_uncleared_and_flagged_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            pending_path = root / "pending.mp4"
            pending_path.write_bytes(b"pending")
            pending = create_asset(db, kind="media", title="Pending", local_path=str(pending_path), rights_status="pending_review", metadata={"duration": 4, "width": 720, "height": 1280})
            project = create_project(db, {"name": "Gate", "theme": "funny"})
            with self.assertRaisesRegex(ValueError, "not cleared"):
                add_project_asset(db, project["id"], pending["id"], {})
            changed_path = root / "changed.mp4"
            changed_path.write_bytes(b"changed")
            changed = create_asset(db, kind="media", title="Changed", local_path=str(changed_path), rights_status="owned_distribution", metadata={"duration": 4, "width": 720, "height": 1280})
            db.update("assets", "id", changed["id"], {"analysis_json": json.dumps({"health": {"status": "changed", "checked_at": "2026-01-01T00:00:00+00:00"}})})
            with self.assertRaisesRegex(ValueError, "changed since import"):
                add_project_asset(db, project["id"], changed["id"], {})
            planned = plan_project(db, project["id"])
            self.assertEqual(planned["assets"], [])

    def test_metadata_content_risk_needs_current_explicit_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            media_path = root / "clip.mp4"
            media_path.write_bytes(b"clip")
            flagged = create_asset(
                db,
                kind="media",
                title="Knife stunt compilation",
                local_path=str(media_path),
                tags=["funny"],
                rights_status="owned_distribution",
                metadata={"duration": 4, "width": 720, "height": 1280},
            )
            self.assertEqual(flagged["content_risk"]["status"], "flagged")
            self.assertEqual(flagged["content_risk"]["basis"], "metadata")
            self.assertIn("content-risk-high", flagged["review_flags"])
            self.assertEqual(create_asset(db, kind="media", title="Funny cats at home", local_path=str(root / "cats.mp4"), rights_status="owned_distribution", metadata={"duration": 2})["content_risk"]["status"], "clear")
            self.assertEqual([asset["id"] for asset in list_assets(db, "content_review")], [flagged["id"]])
            self.assertIn(flagged["id"], [asset["id"] for asset in list_assets(db, "review")])
            project = create_project(db, {"name": "Risk gate", "theme": "funny"})
            with self.assertRaisesRegex(ValueError, "content risk requires review"):
                add_project_asset(db, project["id"], flagged["id"], {})
            kept_flagged = review_content_asset(db, flagged["id"], "flagged", "Keep out of production")
            self.assertEqual(kept_flagged["content_risk"]["status"], "flagged")
            self.assertEqual(list_content_reviews(db, flagged["id"])[0]["verdict"], "flagged")
            with self.assertRaisesRegex(ValueError, "content risk requires review"):
                add_project_asset(db, project["id"], flagged["id"], {})
            safe = review_content_asset(db, flagged["id"], "safe", "Reviewed manually for my private queue")
            self.assertEqual(safe["content_risk"]["status"], "safe")
            self.assertNotIn("content-risk-high", safe["review_flags"])
            self.assertEqual(list_assets(db, "content_review"), [])
            self.assertEqual(list_content_reviews(db, flagged["id"])[0]["note"], "Reviewed manually for my private queue")
            add_project_asset(db, project["id"], flagged["id"], {})
            db.update("assets", "id", flagged["id"], {"title": "Explosion stunt compilation", "updated_at": "2026-01-01T00:00:00+00:00"})
            stale = asset_view(db.get("assets", "id", flagged["id"]) or {})
            self.assertEqual(stale["content_risk"]["status"], "flagged")
            with self.assertRaisesRegex(ValueError, "content risk requires review"):
                add_project_asset(db, project["id"], flagged["id"], {})
            self.assertEqual(review_content_asset(db, flagged["id"], "safe")["content_risk"]["status"], "safe")

    def test_bulk_content_review_is_atomic_and_records_each_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            first = create_asset(db, kind="media", title="Knife clip", local_path=str(root / "first.mp4"), rights_status="owned_distribution", metadata={"duration": 2})
            second = create_asset(db, kind="media", title="Explosion clip", local_path=str(root / "second.mp4"), rights_status="owned_distribution", metadata={"duration": 2})
            reference = create_asset(db, kind="reference", title="Gun reference", source_url="https://example.com/gun")
            with self.assertRaisesRegex(ValueError, "cannot receive a content review"):
                bulk_review_content_assets(db, [first["id"], reference["id"]], "safe", "Mixed selection")
            self.assertEqual(list_content_reviews(db, first["id"]), [])
            reviewed = bulk_review_content_assets(db, [first["id"], second["id"]], "safe", "Batch checked")
            self.assertEqual([asset["content_risk"]["status"] for asset in reviewed], ["safe", "safe"])
            self.assertEqual([list_content_reviews(db, asset["id"])[0]["note"] for asset in reviewed], ["Batch checked", "Batch checked"])

    def test_automation_audio_controls_persist_and_feed_new_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            for name in ("music.m4a", "effect.m4a", "voiceover.m4a"):
                (root / name).write_bytes(b"audio")
            music = create_asset(db, kind="media", title="Music", local_path=str(root / "music.m4a"), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 4, "has_audio": True})
            effect = create_asset(db, kind="media", title="SFX", local_path=str(root / "effect.m4a"), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 1, "has_audio": True})
            voiceover = create_asset(db, kind="media", title="Voiceover", local_path=str(root / "voiceover.m4a"), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 4, "has_audio": True})
            automation = create_automation(db, {"name": "Sounded batch", "watch_dir": directory, "music_asset_id": music["id"], "music_volume": 0, "sfx_asset_id": effect["id"], "sfx_volume": .7, "sfx_start_time": .4, "voiceover_asset_id": voiceover["id"], "voiceover_volume": .8, "voiceover_start_time": 1.2, "auto_render": False})
            self.assertEqual((automation["music_volume"], automation["sfx_start_time"], automation["voiceover_start_time"]), (0.0, 0.4, 1.2))
            video = create_asset(db, kind="media", title="Video", local_path=str(root / "video.mp4"), mime_type="video/mp4", rights_status="owned_distribution", metadata={"duration": 3, "width": 720, "height": 1280}, automation_id=automation["id"])
            with patch("dereel.automation.scan_watch_folder", return_value={"found": 1, "imported": [video], "duplicates": [], "errors": []}), patch("dereel.automation.create_project", return_value={"id": "project_audio"}) as creator, patch("dereel.automation.plan_project", return_value={"assets": [{"asset_id": video["id"]}]}):
                result = run_automation(db, automation["id"], root / "library", root / "outputs")
            self.assertEqual(result["status"], "planned")
            tracks = creator.call_args.args[1]["audio_tracks"]
            self.assertEqual([track["role"] for track in tracks], ["music", "sfx", "voiceover"])
            self.assertEqual((tracks[0]["volume"], tracks[1]["start_time"], tracks[2]["volume"]), (0.0, 0.4, 0.8))

    def test_automation_account_mappings_persist_and_reach_publish_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            for account in (
                {"id": "automation_youtube", "platform": "youtube", "label": "Automation YouTube"},
                {"id": "automation_instagram", "platform": "instagram", "label": "Automation Instagram"},
            ):
                db.insert("publishing_accounts", {**account, "status": "not_connected", "config_json": "{}", "created_at": "2026-01-01", "updated_at": "2026-01-01"})
            automation = create_automation(
                db,
                {
                    "name": "Mapped batch",
                    "watch_dir": str(root),
                    "platforms": ["youtube", "instagram"],
                    "account_ids": {"youtube": "automation_youtube", "instagram": "automation_instagram"},
                },
            )
            self.assertEqual(automation["account_ids"], {"youtube": "automation_youtube", "instagram": "automation_instagram"})
            with self.assertRaisesRegex(ValueError, "does not match youtube"):
                create_automation(db, {"name": "Bad mapping", "watch_dir": str(root), "platforms": ["youtube"], "account_ids": {"youtube": "automation_instagram"}})

            video_path = root / "video.mp4"
            video_path.write_bytes(b"video")
            video = create_asset(db, kind="media", title="Mapped source", local_path=str(video_path), rights_status="owned_distribution", metadata={"duration": 2, "width": 720, "height": 1280}, automation_id=automation["id"])
            with patch("dereel.automation.scan_watch_folder", return_value={"found": 1, "imported": [video], "duplicates": [], "errors": []}), patch("dereel.automation.create_project", return_value={"id": "automation_project"}), patch("dereel.automation.plan_project", return_value={"assets": [{"asset_id": video["id"]}]}), patch("dereel.automation.render_project", return_value={"output_dir": str(root / "output")}), patch("dereel.automation.create_publish_jobs", return_value=[]) as publisher:
                result = run_automation(db, automation["id"], root / "library", root / "outputs")
            self.assertEqual(result["status"], "rendered")
            self.assertEqual(publisher.call_args.kwargs["account_ids"], {"youtube": "automation_youtube", "instagram": "automation_instagram"})
            reduced = update_automation(db, automation["id"], {"platforms": ["youtube"]})
            self.assertEqual(reduced["account_ids"], {"youtube": "automation_youtube"})
            with self.assertRaisesRegex(ValueError, "assigned to automation"):
                delete_publishing_account(db, "automation_youtube")
            delete_publishing_account(db, "automation_instagram")

    def test_automation_retry_reuses_project_when_publish_setup_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Retry release", "watch_dir": str(root), "platforms": ["youtube", "instagram"]})
            video_path = root / "video.mp4"
            video_path.write_bytes(b"video")
            video = create_asset(
                db,
                kind="media",
                title="Retry source",
                local_path=str(video_path),
                mime_type="video/mp4",
                rights_status="owned_distribution",
                metadata={"duration": 4, "width": 720, "height": 1280},
                automation_id=automation["id"],
            )
            output_dir = root / "rendered"
            with patch("dereel.automation.scan_watch_folder", return_value={"found": 1, "imported": [video], "duplicates": [], "errors": []}), patch("dereel.automation.plan_project", return_value={"assets": [{"asset_id": video["id"]}]}), patch("dereel.automation.render_project", return_value={"output_dir": str(output_dir)}), patch("dereel.automation.create_publish_jobs", side_effect=ValueError("account missing")):
                with self.assertRaisesRegex(ValueError, "account missing"):
                    run_automation(db, automation["id"], root / "library", root / "outputs")
            stored_automation = db.get("automations", "id", automation["id"]) or {}
            project_id = stored_automation["last_project_id"]
            self.assertTrue(project_id)
            output_dir.mkdir()
            (output_dir / "final.mp4").write_bytes(b"rendered")
            db.update("projects", "id", project_id, {"status": "ready", "output_dir": str(output_dir)})
            db.insert("publish_jobs", {"id": "failed-youtube", "project_id": project_id, "account_id": None, "platform": "youtube", "status": "failed", "scheduled_for": "", "remote_id": "", "error": "Upload failed", "claim_token": None, "claimed_at": None, "cancelled_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            with patch("dereel.automation.scan_watch_folder", return_value={"found": 0, "imported": [], "duplicates": [], "errors": []}), patch("dereel.automation.create_publish_jobs", return_value=[]) as publisher:
                retried = run_automation(db, automation["id"], root / "library", root / "outputs")
            self.assertEqual((retried["status"], retried["project_id"]), ("rendered", project_id))
            self.assertEqual(publisher.call_count, 1)
            self.assertEqual(publisher.call_args.args[2], ["youtube", "instagram"])
            with db.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

    def test_automation_submission_failure_is_persisted_and_releases_claim(self) -> None:
        class RejectingPool:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("worker pool unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = AppState(
                Settings(
                    root=root,
                    data_dir=root / "data",
                    database_path=root / "data" / "test.sqlite3",
                    originals_dir=root / "library",
                    audio_dir=root / "audio",
                    output_dir=root / "outputs",
                    web_dir=root,
                )
            )
            automation = create_automation(db=app.db, payload={"name": "Rejected", "watch_dir": str(root)})
            with patch("dereel.server.JOB_POOL", RejectingPool()):
                job = app.queue_automation(automation["id"])
            self.assertEqual(job["automation_id"], automation["id"])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error"], "worker pool unavailable")
            stored = app.db.get("automations", "id", automation["id"])
            self.assertEqual(stored["status"], "failed")
            self.assertEqual(stored["last_error"], "worker pool unavailable")

    def test_automation_job_creation_failure_releases_manual_and_scheduled_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = AppState(Settings(root=root, data_dir=root / "data", database_path=root / "data" / "test.sqlite3", originals_dir=root / "library", audio_dir=root / "audio", output_dir=root / "outputs", web_dir=root))
            manual = create_automation(app.db, {"name": "Manual insert failure", "watch_dir": str(root)})
            with patch("dereel.server.create_job", side_effect=RuntimeError("job insert failed")):
                with self.assertRaisesRegex(RuntimeError, "job insert failed"):
                    app.queue_automation(manual["id"])
            stored_manual = app.db.get("automations", "id", manual["id"])
            self.assertEqual((stored_manual["status"], stored_manual["last_error"]), ("failed", "job insert failed"))

            scheduled = create_automation(app.db, {"name": "Scheduled insert failure", "watch_dir": str(root), "enabled": True, "interval_minutes": 15})
            with patch("dereel.server.create_job", side_effect=RuntimeError("scheduled insert failed")):
                app.queue_due_automations()
            stored_scheduled = app.db.get("automations", "id", scheduled["id"])
            self.assertEqual((stored_scheduled["status"], stored_scheduled["last_error"]), ("failed", "scheduled insert failed"))

    def test_restart_recovery_marks_interrupted_render_jobs_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Interrupted", "theme": "recovery"})
            db.update("projects", "id", project["id"], {"status": "rendering"})
            job = create_job(db, project["id"], "render")
            set_job(db, job["id"], status="running", message="Preparing render")
            automation = create_automation(db, {"name": "Interrupted automation", "watch_dir": str(root)})
            db.update("automations", "id", automation["id"], {"status": "running"})
            automation_job = create_job(db, None, "automation", automation["id"])
            set_job(db, automation_job["id"], status="running", message="Scanning watch folder")
            automation_run = create_automation_run(db, automation["id"], automation_job["id"])
            recovered = recover_interrupted_jobs(db)
            self.assertEqual(recovered, [job["id"], automation_job["id"]])
            stored_job = db.get("jobs", "id", job["id"])
            self.assertEqual((stored_job["status"], stored_job["error"]), ("failed", "Interrupted by process restart"))
            self.assertEqual(db.get("projects", "id", project["id"])["status"], "failed")
            stored_automation_job = db.get("jobs", "id", automation_job["id"])
            self.assertEqual((stored_automation_job["status"], stored_automation_job["error"]), ("failed", "Interrupted by process restart"))
            stored_automation = db.get("automations", "id", automation["id"])
            self.assertEqual((stored_automation["status"], stored_automation["last_error"]), ("failed", "Interrupted by process restart"))
            stored_run = list_automation_runs(db, automation["id"])[0]
            self.assertEqual((stored_run["id"], stored_run["status"], stored_run["error"]), (automation_run["id"], "failed", "Interrupted by process restart"))

    def test_automation_run_history_persists_report_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            automation = create_automation(db, {"name": "History", "watch_dir": directory})
            project = create_project(db, {"name": "History project", "theme": "history"})
            job = create_job(db, None, "automation", automation["id"])
            run = create_automation_run(db, automation["id"], job["id"])
            finished = finish_automation_run(
                db,
                run["id"],
                result={"status": "awaiting_review", "project_id": project["id"], "scan": {"found": 3, "imported": [{"id": "asset_1"}], "duplicates": ["old.mp4"], "errors": []}},
            )
            self.assertEqual((finished["status"], finished["project_id"], finished["scan"]["found"]), ("awaiting_review", project["id"], 3))
            self.assertEqual(list_automation_runs(db, automation["id"])[0]["result"]["status"], "awaiting_review")
            failed_job = create_job(db, None, "automation", automation["id"])
            failed_run = create_automation_run(db, automation["id"], failed_job["id"])
            failed = finish_automation_run(db, failed_run["id"], error="Provider unavailable")
            self.assertEqual((failed["status"], failed["error"]), ("failed", "Provider unavailable"))

    def test_automation_worker_links_completed_job_to_run_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = AppState(Settings(root=root, data_dir=root / "data", database_path=root / "data" / "test.sqlite3", originals_dir=root / "library", audio_dir=root / "audio", output_dir=root / "outputs", web_dir=root))
            automation = create_automation(app.db, {"name": "Worker history", "watch_dir": str(root)})
            job = create_job(app.db, None, "automation", automation["id"])
            with patch("dereel.server.run_automation", return_value={"status": "awaiting_review", "scan": {"found": 1, "imported": [], "duplicates": [], "errors": []}}):
                app.execute_automation(job["id"], automation["id"])
            stored_job = app.db.get("jobs", "id", job["id"])
            self.assertEqual(stored_job["status"], "completed")
            run_id = json.loads(stored_job["result_json"])["run_id"]
            run = list_automation_runs(app.db, automation["id"])[0]
            self.assertEqual((run["status"], run["job_id"], run["scan"]["found"]), ("awaiting_review", job["id"], 1))
            self.assertEqual(run_id, run["id"])

    def test_failed_render_retry_creates_a_new_guarded_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Retry me", "theme": "recovery"})
            failed = create_job(db, project["id"], "render")
            set_job(db, failed["id"], status="failed", message="Render failed", error="temporary failure")
            retry = retry_failed_job(db, failed["id"])
            self.assertEqual((retry["retry_of"], retry["attempt"], retry["status"]), (failed["id"], 2, "queued"))
            with self.assertRaisesRegex(ValueError, "latest failed attempt"):
                retry_failed_job(db, failed["id"])
            with self.assertRaisesRegex(ValueError, "Only failed jobs"):
                retry_failed_job(db, retry["id"])

    def test_render_queue_rejects_duplicate_active_work_and_persists_submit_failure(self) -> None:
        class RejectingPool:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("render pool unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = AppState(Settings(root=root, data_dir=root / "data", database_path=root / "data" / "test.sqlite3", originals_dir=root / "library", audio_dir=root / "audio", output_dir=root / "outputs", web_dir=root))
            project = create_project(app.db, {"name": "Render queue", "theme": "test"})
            with patch("dereel.server.JOB_POOL", RejectingPool()):
                job = app.queue_render(project["id"])
            self.assertEqual((job["status"], job["error"]), ("failed", "render pool unavailable"))
            with patch("dereel.server.JOB_POOL", RejectingPool()):
                retry = app.retry_job(job["id"])
            self.assertEqual((retry["attempt"], retry["retry_of"], retry["status"]), (2, job["id"], "failed"))

    def test_render_queue_reserves_project_before_worker_submission(self) -> None:
        class HoldingPool:
            def submit(self, *_args, **_kwargs):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = AppState(Settings(root=root, data_dir=root / "data", database_path=root / "data" / "test.sqlite3", originals_dir=root / "library", audio_dir=root / "audio", output_dir=root / "outputs", web_dir=root))
            project = create_project(app.db, {"name": "Atomic render", "theme": "queue"})
            with patch("dereel.server.JOB_POOL", HoldingPool()):
                job = app.queue_render(project["id"])
                with self.assertRaisesRegex(ValueError, "already processing"):
                    app.queue_render(project["id"])
            self.assertEqual((job["status"], app.db.get("projects", "id", project["id"])["status"]), ("queued", "rendering"))

    def test_render_rejects_a_pending_publish_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = AppState(Settings(root=root, data_dir=root / "data", database_path=root / "data" / "test.sqlite3", originals_dir=root / "library", audio_dir=root / "audio", output_dir=root / "outputs", web_dir=root))
            project = create_project(app.db, {"name": "Release lock", "theme": "queue"})
            with app.db.connection() as connection:
                connection.execute(
                    "INSERT INTO publish_jobs(id, project_id, platform, status, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, '{}', ?, ?)",
                    ("pending_render_lock", project["id"], "youtube", "ready", "2026-01-01", "2026-01-01"),
                )
            with self.assertRaisesRegex(ValueError, "pending publish job"):
                app.queue_render(project["id"])
            with self.assertRaisesRegex(ValueError, "pending publish job"):
                render_project(app.db, project["id"], root / "outputs", lambda _progress, _message: None)

    def test_planning_failure_marks_project_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            source = root / "clip.mp4"
            source.write_bytes(b"clip")
            create_asset(db, kind="media", title="Clip", local_path=str(source), mime_type="video/mp4", rights_status="owned_distribution", metadata={"duration": 2, "width": 720, "height": 1280})
            project = create_project(db, {"name": "Planner recovery", "theme": "clip"})
            with patch("dereel.pipeline.score_breakdown", side_effect=RuntimeError("planner failed")):
                with self.assertRaisesRegex(RuntimeError, "planner failed"):
                    plan_project(db, project["id"])
            self.assertEqual(db.get("projects", "id", project["id"])["status"], "failed")

    def test_render_failure_marks_project_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            source = root / "clip.mp4"
            source.write_bytes(b"clip")
            asset = create_asset(db, kind="media", title="Clip", local_path=str(source), mime_type="video/mp4", rights_status="owned_distribution", metadata={"duration": 2, "width": 720, "height": 1280})
            project = create_project(db, {"name": "Render recovery", "theme": "clip"})
            old_output = root / "outputs" / project["id"] / "old"
            old_output.mkdir(parents=True)
            (old_output / "final.mp4").write_bytes(b"old package")
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(old_output), "manifest_json": json.dumps({"validation": {"status": "passed"}})})
            with db.connection() as connection:
                connection.execute("INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, enabled, selected_by) VALUES (?, ?, 0, 0, 2, 1, 'test')", (project["id"], asset["id"]))
            with patch("dereel.pipeline.validate_video", return_value={"duration": 2, "file_size": 4, "width": 720, "height": 1280, "has_audio": False}), patch("dereel.pipeline.normalize_segment", side_effect=RuntimeError("render failed")):
                with self.assertRaisesRegex(RuntimeError, "render failed"):
                    render_project(db, project["id"], root / "outputs", lambda _progress, _message: None)
            stored = db.get("projects", "id", project["id"])
            self.assertEqual((stored["status"], stored["output_dir"], stored["manifest_json"]), ("failed", "", "{}"))
            self.assertTrue((old_output / "final.mp4").is_file())

    def test_render_rechecks_source_fingerprint_before_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "clip.mp4"
            source.write_bytes(b"original")
            original_fingerprint = sha256_file(source)
            db = Database(root / "test.sqlite3")
            asset = create_asset(
                db,
                kind="media",
                title="Replaced clip",
                local_path=str(source),
                mime_type="video/mp4",
                rights_status="owned_distribution",
                metadata={"duration": 2, "width": 720, "height": 1280, "fingerprint": original_fingerprint},
            )
            source.write_bytes(b"replacement")
            project = create_project(db, {"name": "Live source gate", "theme": "clip"})
            with db.connection() as connection:
                connection.execute(
                    "INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, enabled, selected_by) VALUES (?, ?, 0, 0, 2, 1, 'test')",
                    (project["id"], asset["id"]),
                )
            with patch("dereel.pipeline.validate_video", return_value={"duration": 2, "file_size": 11, "width": 720, "height": 1280, "has_audio": False}):
                with self.assertRaisesRegex(ValueError, "changed since import"):
                    render_project(db, project["id"], root / "outputs", lambda _progress, _message: None)
            stored_asset = db.get("assets", "id", asset["id"]) or {}
            self.assertEqual(json.loads(stored_asset["analysis_json"])["health"]["status"], "changed")

    def test_provider_automation_requires_query_and_keeps_imports_reviewable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            with self.assertRaisesRegex(ValueError, "search query"):
                create_automation(db, {"name": "Missing query", "source_mode": "provider", "provider": "pexels"})
            automation = create_automation(db, {"name": "Provider batch", "source_mode": "provider", "provider": "pexels", "search_query": "funny cats"})
            with patch("dereel.automation.search_provider", return_value=[{"provider": "pexels", "provider_id": "1", "download_url": "https://videos.pexels.com/clip.mp4", "source_url": "https://pexels.com/video/1", "title": "Provider clip"}]), patch("dereel.automation.import_provider_asset", return_value={"id": "asset_1", "rights_status": "pending_review"}) as importer:
                result = scan_provider_sources(db, automation, Path(directory) / "library")
            self.assertEqual(len(result["imported"]), 1)
            self.assertEqual(result["imported"][0]["rights_status"], "pending_review")
            self.assertEqual(importer.call_args.kwargs["automation_id"], automation["id"])

    def test_bulk_provider_import_preserves_successes_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            items = [
                {"provider": "pexels", "provider_id": "one", "download_url": "https://videos.pexels.com/one.mp4", "title": "One"},
                {"provider": "pexels", "provider_id": "two", "download_url": "https://videos.pexels.com/two.mp4", "title": "Two"},
            ]
            with patch("dereel.scout.import_provider_asset", side_effect=[{"id": "asset_one", "rights_status": "pending_review"}, ValueError("duplicate provider result")]) as importer:
                result = import_provider_assets(db, Path(directory) / "library", items)
            self.assertEqual([asset["id"] for asset in result["imported"]], ["asset_one"])
            self.assertEqual((result["errors"][0]["provider_id"], result["errors"][0]["error"]), ("two", "duplicate provider result"))
            self.assertEqual(importer.call_count, 2)
            with self.assertRaisesRegex(ValueError, "at most 24"):
                import_provider_assets(db, Path(directory) / "library", items * 13)

    def test_owned_provider_automation_records_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Licensed provider", "source_mode": "provider", "provider": "pexels", "search_query": "funny cats", "default_rights": "licensed_distribution"})
            imported = create_asset(db, kind="media", title="Provider clip", source_url="https://pexels.com/video/1", platform="pexels", local_path=str(root / "provider.mp4"), mime_type="video/mp4", rights_status="pending_review", metadata={"duration": 2, "width": 720, "height": 1280})
            with patch("dereel.automation.search_provider", return_value=[{"provider": "pexels", "provider_id": "1", "download_url": "https://videos.pexels.com/clip.mp4", "source_url": "https://pexels.com/video/1", "title": "Provider clip"}]), patch("dereel.automation.import_provider_asset", return_value=imported):
                result = scan_provider_sources(db, automation, root / "library")
            asset = result["imported"][0]
            self.assertEqual((asset["rights_status"], asset["status"]), ("licensed_distribution", "approved"))
            history = list_asset_reviews(db, asset["id"])
            self.assertEqual((history[0]["action"], history[0]["previous_rights_status"], history[0]["note"]), ("approve_licensed", "pending_review", "Automation attestation: licensed distribution"))

    def test_provider_attestation_failure_cleans_up_imported_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Provider cleanup", "source_mode": "provider", "provider": "pexels", "search_query": "funny cats", "default_rights": "licensed_distribution"})
            provider_path = root / "provider.mp4"
            provider_path.write_bytes(b"provider")
            imported = create_asset(db, kind="media", title="Provider clip", source_url="https://pexels.com/video/1", platform="pexels", local_path=str(provider_path), rights_status="pending_review", metadata={"duration": 2, "width": 720, "height": 1280})
            with patch("dereel.automation.search_provider", return_value=[{"provider": "pexels", "provider_id": "1", "download_url": "https://videos.pexels.com/clip.mp4", "source_url": "https://pexels.com/video/1", "title": "Provider clip"}]), patch("dereel.automation.import_provider_asset", return_value=imported), patch("dereel.automation.review_asset", side_effect=RuntimeError("review database unavailable")):
                result = scan_provider_sources(db, automation, root / "library")
            self.assertEqual(len(result["errors"]), 1)
            self.assertIsNone(db.get("assets", "id", imported["id"]))
            self.assertFalse(provider_path.exists())

    def test_provider_cleanup_preserves_a_concurrent_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Provider race", "source_mode": "provider", "provider": "pexels", "search_query": "funny cats", "default_rights": "licensed_distribution"})
            provider_path = root / "provider.mp4"
            provider_path.write_bytes(b"provider")
            imported = create_asset(db, kind="media", title="Provider clip", source_url="https://pexels.com/video/1", platform="pexels", local_path=str(provider_path), rights_status="pending_review", metadata={"duration": 2, "width": 720, "height": 1280})

            def approve_then_fail(*_args: object, **_kwargs: object) -> dict[str, Any]:
                review_asset(db, imported["id"], "approve_licensed", "Concurrent approval")
                raise RuntimeError("response serialization failed")

            with patch("dereel.automation.search_provider", return_value=[{"provider": "pexels", "provider_id": "1", "download_url": "https://videos.pexels.com/clip.mp4", "source_url": "https://pexels.com/video/1", "title": "Provider clip"}]), patch("dereel.automation.import_provider_asset", return_value=imported), patch("dereel.automation.review_asset", side_effect=approve_then_fail):
                result = scan_provider_sources(db, automation, root / "library")
            self.assertEqual(len(result["errors"]), 1)
            preserved = db.get("assets", "id", imported["id"])
            self.assertEqual((preserved["rights_status"], preserved["status"]), ("licensed_distribution", "approved"))
            self.assertTrue(provider_path.exists())

    def test_project_defaults_to_vertical_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Cat cuts", "theme": "cats funny"})
            self.assertEqual(project["format"], "vertical_9_16")
            self.assertEqual(project["status"], "draft")
            self.assertEqual(project["assets"], [])
            self.assertTrue(project["settings"]["add_title_card"])
            self.assertTrue(project["settings"]["write_captions"])

    def test_project_creation_rolls_back_when_audio_link_insert_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            audio_path = root / "bed.m4a"
            audio_path.write_bytes(b"audio")
            audio = create_asset(db, kind="media", title="Music bed", local_path=str(audio_path), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 4})
            with db.connection() as connection:
                connection.execute(
                    "CREATE TRIGGER fail_project_audio BEFORE INSERT ON project_audio BEGIN SELECT RAISE(ABORT, 'forced audio link failure'); END"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced audio link failure"):
                create_project(db, {"name": "Atomic project", "theme": "cats", "audio_tracks": [{"asset_id": audio["id"], "role": "music"}]})
            with db.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_audio").fetchone()[0], 0)

    def test_project_text_treatment_settings_normalize_boolean_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "No treatment", "theme": "quiet", "add_title_card": "false", "write_captions": 0})
            self.assertEqual((project["settings"]["add_title_card"], project["settings"]["write_captions"]), (False, False))

    def test_project_audio_selection_rejects_flagged_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            audio_path = root / "changed.m4a"
            audio_path.write_bytes(b"audio")
            audio = create_asset(db, kind="media", title="Changed bed", local_path=str(audio_path), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 4, "health": {"status": "changed", "problems": ["File fingerprint changed since import"]}})
            with self.assertRaisesRegex(ValueError, "not ready for production: media file changed since import"):
                create_project(db, {"name": "Audio gate", "theme": "cats", "audio_tracks": [{"asset_id": audio["id"], "role": "music"}]})

    def test_legacy_project_audio_settings_are_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            audio_path = root / "legacy.m4a"
            audio_path.write_bytes(b"audio")
            audio = create_asset(db, kind="media", title="Legacy music", local_path=str(audio_path), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 4})
            project = create_project(db, {"name": "Legacy audio", "theme": "cats"})
            db.update("projects", "id", project["id"], {"settings_json": json.dumps({"music_asset_id": audio["id"], "music_volume": 0.25})})
            visible = get_project_audio(db, project["id"])
            self.assertEqual([(track["role"], track["asset_id"], track["volume"]) for track in visible], [("music", audio["id"], 0.25)])

    def test_project_settings_update_replaces_audio_and_invalidates_old_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            music_path = root / "music.m4a"
            music_path.write_bytes(b"audio")
            music = create_asset(db, kind="media", title="Music", local_path=str(music_path), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 8, "has_audio": True})
            project = create_project(db, {"name": "Before", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(root / "old-output"), "manifest_json": json.dumps({"validation": {"status": "passed"}})})
            updated = update_project(db, project["id"], {"name": "After", "theme": "cats funny", "title": "Cats in trouble", "caption": "Perfect timing.", "duration_target": 42, "style": "clean", "add_title_card": False, "write_captions": True, "audio_tracks": [{"asset_id": music["id"], "role": "music", "volume": 0.2}]})
            stored = db.get("projects", "id", project["id"])
            self.assertEqual((updated["name"], updated["duration_target"], updated["style"]), ("After", 42.0, "clean"))
            self.assertEqual((stored["status"], stored["output_dir"], stored["manifest_json"]), ("draft", "", "{}"))
            self.assertEqual([(track["asset_id"], track["role"], track["volume"]) for track in updated["audio"]], [(music["id"], "music", 0.2)])
            self.assertFalse(updated["settings"]["add_title_card"])

    def test_project_edits_are_blocked_while_publish_job_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Release lock", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(root / "output")})
            db.insert("publish_jobs", {"id": "publish_lock", "project_id": project["id"], "account_id": None, "platform": "youtube", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "cancelled_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            with self.assertRaisesRegex(ValueError, "pending publish job"):
                update_project(db, project["id"], {"name": "Changed"})
            with self.assertRaisesRegex(ValueError, "pending publish job"):
                plan_project(db, project["id"])
            db.update("publish_jobs", "id", "publish_lock", {"status": "failed", "cancelled_at": "2026-01-01T00:00:00+00:00"})
            changed = update_project(db, project["id"], {"name": "Changed"})
            self.assertEqual(changed["name"], "Changed")

    def test_publishing_requires_a_fresh_ready_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Draft release", "theme": "cats"})
            with self.assertRaisesRegex(ValueError, "must be rendered and ready"):
                create_publish_jobs(db, project, ["youtube"])

    def test_publisher_account_and_outbox_lifecycle_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            account_id = "account_lifecycle"
            project = create_project(db, {"name": "Release", "theme": "cats"})
            db.insert("publishing_accounts", {"id": account_id, "platform": "instagram", "label": "Old label", "status": "not_connected", "config_json": json.dumps({"connection_check": {"status": "passed", "identity": "stale"}}), "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            updated_account = update_publishing_account(db, account_id, {"label": "Main Reels", "instagram_user_id": "ig_123", "public_media_base_url": "https://media.example"})
            self.assertEqual((updated_account["label"], updated_account["config"]["instagram_user_id"]), ("Main Reels", "ig_123"))
            self.assertEqual(updated_account["connection_check"], {})
            job_id = "outbox_lifecycle"
            output_dir = root / "output"
            output_dir.mkdir()
            media_path = output_dir / "instagram.mp4"
            media_path.write_bytes(b"export")
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": account_id, "platform": "instagram", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            db.update("publish_jobs", "id", job_id, {"payload_json": json.dumps({"media_path": str(media_path), "title": "Release"})})
            with self.assertRaisesRegex(ValueError, "pending publish job"):
                delete_publishing_account(db, account_id)
            scheduled = reschedule_publish_job(db, job_id, "2026-02-01T12:00:00+00:00")
            self.assertEqual(scheduled["scheduled_for"], "2026-02-01T12:00:00+00:00")
            package_manifest = json.loads((output_dir / "publish-manifest.json").read_text())
            self.assertEqual(package_manifest["jobs"][0]["scheduled_for"], "2026-02-01T12:00:00+00:00")
            cancelled = cancel_publish_job(db, job_id)
            self.assertTrue(cancelled["cancelled"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertFalse(claim_publish_job(db, job_id))
            delete_publishing_account(db, account_id)
            self.assertIsNone(db.get("publishing_accounts", "id", account_id))

    def test_delete_project_cleans_package_keeps_sources_and_blocks_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            (root / "source.mp4").write_bytes(b"source")
            asset = create_asset(db, kind="media", title="Shared source", local_path=str(root / "source.mp4"), rights_status="owned_distribution", metadata={"duration": 4, "width": 720, "height": 1280})
            project = create_project(db, {"name": "Disposable", "theme": "cleanup"})
            add_project_asset(db, project["id"], asset["id"], {})
            job = create_job(db, project["id"], "render")
            with self.assertRaisesRegex(ValueError, "while a job is processing"):
                delete_project(db, project["id"], root / "outputs")
            set_job(db, job["id"], status="completed", message="Done")
            package = root / "outputs" / project["id"] / "run-1"
            package.mkdir(parents=True)
            (package / "final.mp4").write_bytes(b"old")
            delete_project(db, project["id"], root / "outputs")
            self.assertIsNone(db.get("projects", "id", project["id"]))
            self.assertIsNotNone(db.get("assets", "id", asset["id"]))
            self.assertFalse((root / "outputs" / project["id"]).exists())

    def test_delete_project_preserves_publish_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Published project", "theme": "history"})
            db.insert("publish_jobs", {"id": "published_project_job", "project_id": project["id"], "account_id": None, "platform": "youtube", "status": "published", "scheduled_for": "", "remote_id": "video-1", "error": "", "claim_token": None, "claimed_at": None, "cancelled_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            with self.assertRaisesRegex(ValueError, "publish history"):
                delete_project(db, project["id"], root / "outputs")
            self.assertIsNotNone(db.get("projects", "id", project["id"]))

    def test_asset_delete_removes_unused_media_and_preserves_referenced_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            unused_path = root / "unused.mp4"
            unused_path.write_bytes(b"unused")
            unused = create_asset(db, kind="media", title="Unused", local_path=str(unused_path), rights_status="pending_review", metadata={"duration": 2, "width": 720, "height": 1280})
            delete_asset(db, unused["id"])
            self.assertFalse(unused_path.exists())
            self.assertIsNone(db.get("assets", "id", unused["id"]))

            audio_path = root / "audio.m4a"
            audio_path.write_bytes(b"audio")
            audio = create_asset(db, kind="media", title="Audio", local_path=str(audio_path), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 4, "has_audio": True})
            project = create_project(db, {"name": "Audio reference", "theme": "sound", "audio_tracks": [{"asset_id": audio["id"], "role": "music"}]})
            with self.assertRaisesRegex(ValueError, "used by a project"):
                delete_asset(db, audio["id"])
            self.assertTrue(audio_path.exists())
            delete_project(db, project["id"])

            automation_audio_path = root / "automation-audio.m4a"
            automation_audio_path.write_bytes(b"audio")
            automation_audio = create_asset(db, kind="media", title="Automation audio", local_path=str(automation_audio_path), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 4, "has_audio": True})
            automation = create_automation(db, {"name": "Audio recipe", "watch_dir": directory, "music_asset_id": automation_audio["id"]})
            with self.assertRaisesRegex(ValueError, "assigned to automation"):
                delete_asset(db, automation_audio["id"])
            self.assertTrue(automation_audio_path.exists())
            self.assertIsNotNone(db.get("automations", "id", automation["id"]))

    def test_pending_asset_cleanup_only_removes_unapproved_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            pending_path = root / "pending.mp4"
            pending_path.write_bytes(b"pending")
            pending = create_asset(db, kind="media", title="Pending", local_path=str(pending_path), rights_status="pending_review", metadata={"duration": 2})
            cleanup_pending_asset(db, pending["id"])
            self.assertIsNone(db.get("assets", "id", pending["id"]))
            self.assertFalse(pending_path.exists())

            approved_path = root / "approved.mp4"
            approved_path.write_bytes(b"approved")
            approved = create_asset(db, kind="media", title="Approved", local_path=str(approved_path), rights_status="owned_distribution", metadata={"duration": 2})
            cleanup_pending_asset(db, approved["id"])
            self.assertIsNotNone(db.get("assets", "id", approved["id"]))
            self.assertTrue(approved_path.exists())

    def test_timeline_moves_clips_and_marks_ready_projects_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            for index in range(3):
                (root / f"clip-{index}.mp4").write_bytes(b"source")
            project = create_project(db, {"name": "Ordered cut", "theme": "sequence"})
            assets = [
                create_asset(
                    db,
                    kind="media",
                    title=f"Clip {index}",
                    local_path=str(root / f"clip-{index}.mp4"),
                    rights_status="owned_distribution",
                    metadata={"duration": 4, "width": 720, "height": 1280},
                )
                for index in range(3)
            ]
            for asset in assets:
                add_project_asset(db, project["id"], asset["id"], {})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(root / "old-output"), "manifest_json": json.dumps({"validation": {"status": "passed"}})})
            move_project_asset(db, project["id"], assets[2]["id"], "up")
            self.assertEqual([item["asset_id"] for item in get_project_assets(db, project["id"])], [assets[0]["id"], assets[2]["id"], assets[1]["id"]])
            self.assertEqual(db.get("projects", "id", project["id"])["status"], "draft")
            self.assertEqual((db.get("projects", "id", project["id"])["output_dir"], db.get("projects", "id", project["id"])["manifest_json"]), ("", "{}"))
            update_project_asset(db, project["id"], assets[0]["id"], {"in_point": 0.5, "out_point": 3.5})
            self.assertEqual(db.get("projects", "id", project["id"])["status"], "draft")
            move_project_asset(db, project["id"], assets[0]["id"], "up")
            self.assertEqual([item["asset_id"] for item in get_project_assets(db, project["id"])], [assets[0]["id"], assets[2]["id"], assets[1]["id"]])
            remove_project_asset(db, project["id"], assets[2]["id"])
            self.assertEqual([item["asset_id"] for item in get_project_assets(db, project["id"])], [assets[0]["id"], assets[1]["id"]])

    def test_score_prefers_theme_matches_and_short_clips(self) -> None:
        matching = {"title": "cat surprise", "tags_json": '["cats","funny"]', "duration": 6, "width": 1080, "height": 1920, "has_audio": 1}
        weak = {"title": "landscape", "tags_json": "[]", "duration": 45, "width": 1920, "height": 1080, "has_audio": 0}
        self.assertGreater(score_asset(matching, {"cat", "funny"}), score_asset(weak, {"cat", "funny"}))
        breakdown = score_breakdown(matching, {"cat", "funny"})
        self.assertEqual(breakdown["theme_matches"], ["cat", "funny"])
        self.assertEqual(breakdown["duration_bucket"], "short")

    def test_provider_import_only_allows_known_https_hosts(self) -> None:
        self.assertTrue(_host_allowed("pexels", "https://videos.pexels.com/video.mp4"))
        self.assertTrue(_host_allowed("pixabay", "https://cdn.pixabay.com/video.mp4"))
        self.assertFalse(_host_allowed("pexels", "https://example.com/video.mp4"))
        self.assertFalse(_host_allowed("pexels", "http://videos.pexels.com/video.mp4"))

    def test_provider_search_retries_transient_http_failures(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"videos":[]}'

        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            if len(calls) < 3:
                return_value = urllib_error.HTTPError(request.full_url, 503, "busy", {}, io.BytesIO(b"busy"))
                raise return_value
            return FakeResponse()

        with patch("dereel.scout.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("dereel.scout.time.sleep") as sleep:
                payload = _request_json("https://api.example/search")

        self.assertEqual(payload, {"videos": []})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_provider_download_retries_transient_network_failure(self) -> None:
        class Headers:
            def get_content_type(self) -> str:
                return "video/mp4"

        class FakeResponse:
            headers = Headers()

            def __init__(self, fail_on_second_read: bool = False):
                self.read_calls = 0
                self.fail_on_second_read = fail_on_second_read

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self) -> str:
                return "https://videos.pexels.com/clip.mp4"

            def read(self, _size: int) -> bytes:
                self.read_calls += 1
                if self.fail_on_second_read and self.read_calls == 2:
                    raise urllib_error.URLError("temporary stream interruption")
                return b"video" if self.read_calls == 1 else b""

        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(fail_on_second_read=len(calls) == 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            with patch("dereel.scout.urllib.request.urlopen", side_effect=fake_urlopen), patch("dereel.scout.validate_video", return_value={"duration": 2, "width": 720, "height": 1280}), patch("dereel.scout.sha256_file", return_value="provider-fingerprint"), patch("dereel.scout.create_asset", return_value={"id": "asset_provider"}) as creator:
                with patch("dereel.scout.time.sleep") as sleep:
                    result = import_provider_asset(db, root / "library", {"provider": "pexels", "provider_id": "1", "download_url": "https://videos.pexels.com/clip.mp4", "source_url": "https://www.pexels.com/video/1", "title": "Provider clip"})

            self.assertEqual(result["id"], "asset_provider")
            self.assertEqual(len(calls), 2)
            self.assertEqual(sleep.call_count, 1)
            self.assertEqual([path.suffix for path in (root / "library").iterdir()], [".mp4"])
            self.assertEqual(list((root / "library").glob("*.part")), [])
            creator.assert_called_once()

    def test_provider_import_skips_existing_source_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            existing_path = root / "existing.mp4"
            existing_path.write_bytes(b"existing")
            create_asset(
                db,
                kind="media",
                title="Existing provider clip",
                source_url="https://pexels.com/video/1",
                platform="pexels",
                local_path=str(existing_path),
                rights_status="pending_review",
                metadata={"fingerprint": "existing"},
            )
            with patch("dereel.scout.urllib.request.urlopen") as opener:
                with self.assertRaisesRegex(ValueError, "matches existing asset"):
                    import_provider_asset(
                        db,
                        root / "library",
                        {
                            "provider": "pexels",
                            "provider_id": "1",
                            "download_url": "https://videos.pexels.com/video.mp4",
                            "source_url": "https://pexels.com/video/1",
                        },
                    )
            opener.assert_not_called()

    def test_publisher_capabilities_are_explicit(self) -> None:
        capabilities = {item["id"]: item for item in platform_capabilities()}
        self.assertEqual(capabilities["youtube"]["mode"], "direct_upload")
        self.assertEqual(capabilities["instagram"]["mode"], "public_url_container")
        self.assertEqual(capabilities["tiktok"]["mode"], "inbox_draft_handoff")
        self.assertEqual(account_view({"id": "account_1", "platform": "youtube", "label": "Main", "config_json": "{}", "status": "not_connected"})["status"], "not_connected")

    def test_publisher_account_readiness_reports_platform_specific_blockers(self) -> None:
        instagram = {"id": "account_ig", "platform": "instagram", "label": "Reels", "config_json": json.dumps({"instagram_user_id": "ig_123", "public_media_base_url": "https://media.example"})}
        with patch.dict("os.environ", {}, clear=True):
            unavailable = account_view(instagram)
        self.assertFalse(unavailable["readiness"]["ready"])
        self.assertIn("Set DEREEL_INSTAGRAM_ACCESS_TOKEN", unavailable["readiness"]["issues"])
        with patch.dict("os.environ", {"DEREEL_INSTAGRAM_ACCESS_TOKEN": "token"}, clear=True):
            ready = account_view(instagram)
        self.assertTrue(ready["readiness"]["ready"])
        with patch.dict("os.environ", {"DEREEL_INSTAGRAM_ACCESS_TOKEN": "token"}, clear=True):
            incomplete = account_view({"id": "account_incomplete", "platform": "instagram", "label": "Incomplete", "config_json": "{}"})
        self.assertFalse(incomplete["readiness"]["ready"])
        self.assertIn("Instagram professional account ID is missing", incomplete["readiness"]["issues"])
        with patch.dict("os.environ", {"DEREEL_INSTAGRAM_ACCESS_TOKEN": "token"}, clear=True):
            invalid_url = account_view({"id": "account_invalid_url", "platform": "instagram", "label": "Invalid URL", "config_json": json.dumps({"instagram_user_id": "ig_123", "public_media_base_url": "https://"})})
        self.assertFalse(invalid_url["readiness"]["ready"])
        self.assertIn("HTTPS public media base URL is missing", invalid_url["readiness"]["issues"])
        with patch.dict("os.environ", {"DEREEL_INSTAGRAM_ACCESS_TOKEN": "   "}, clear=True):
            whitespace_token = account_view(instagram)
        self.assertFalse(whitespace_token["readiness"]["token_configured"])

    def test_account_connection_check_blocks_without_request_and_persists_safe_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            account = {"id": "account_youtube", "platform": "youtube", "label": "Main", "config_json": "{}", "status": "not_connected"}
            db.insert("publishing_accounts", {**account, "created_at": "2026-01-01", "updated_at": "2026-01-01"})
            with patch.dict("os.environ", {}, clear=True), patch("dereel.publisher.urllib.request.urlopen") as opener:
                checked = check_publishing_account(db, account["id"])
            opener.assert_not_called()
            self.assertEqual(checked["connection_check"]["status"], "blocked")
            self.assertIn("Set DEREEL_YOUTUBE_ACCESS_TOKEN", checked["connection_check"]["message"])
            stored = json.loads(db.get("publishing_accounts", "id", account["id"])["config_json"])
            self.assertNotIn("access_token", stored)
            self.assertNotIn("secret-token", json.dumps(stored))

    def test_account_connection_check_reads_youtube_identity_without_upload(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"items":[{"id":"channel_1","snippet":{"title":"Dewflash"}}]}'

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            account = {"id": "account_youtube", "platform": "youtube", "label": "Main", "config_json": "{}", "status": "not_connected"}
            db.insert("publishing_accounts", {**account, "created_at": "2026-01-01", "updated_at": "2026-01-01"})
            with patch.dict("os.environ", {"DEREEL_YOUTUBE_ACCESS_TOKEN": "secret-token"}, clear=True), patch("dereel.publisher.urllib.request.urlopen", return_value=FakeResponse()) as opener:
                checked = check_publishing_account(db, account["id"])
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, "https://www.googleapis.com/youtube/v3/channels?part=id%2Csnippet&mine=true")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
            self.assertEqual((checked["connection_check"]["status"], checked["connection_check"]["identity"]), ("passed", "Dewflash"))

    def test_account_connection_check_uses_instagram_and_tiktok_identity_requests(self) -> None:
        class FakeResponse:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return self.body

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.insert("publishing_accounts", {"id": "account_instagram", "platform": "instagram", "label": "Reels", "status": "not_connected", "config_json": json.dumps({"instagram_user_id": "ig_123", "public_media_base_url": "https://media.example"}), "created_at": "2026-01-01", "updated_at": "2026-01-01"})
            db.insert("publishing_accounts", {"id": "account_tiktok", "platform": "tiktok", "label": "TikTok", "status": "not_connected", "config_json": "{}", "created_at": "2026-01-01", "updated_at": "2026-01-01"})
            with patch.dict("os.environ", {"DEREEL_INSTAGRAM_ACCESS_TOKEN": "ig-secret", "DEREEL_TIKTOK_ACCESS_TOKEN": "tt-secret"}, clear=True), patch("dereel.publisher.urllib.request.urlopen", side_effect=[FakeResponse(b'{"id":"ig_123","username":"reels"}'), FakeResponse(b'{"data":{"open_id":"open_1","display_name":"Dewflash"}}')]) as opener:
                instagram = check_publishing_account(db, "account_instagram")
                tiktok = check_publishing_account(db, "account_tiktok")
            self.assertEqual(opener.call_args_list[0].args[0].full_url, "https://graph.facebook.com/v22.0/ig_123?fields=id%2Cusername")
            self.assertEqual(opener.call_args_list[1].args[0].full_url, "https://open.tiktokapis.com/v2/user/info/?fields=open_id%2Cdisplay_name")
            self.assertEqual((instagram["connection_check"]["identity"], tiktok["connection_check"]["identity"]), ("reels", "Dewflash"))

    def test_runtime_diagnostics_reports_media_and_provider_checks(self) -> None:
        diagnostics = runtime_diagnostics()
        self.assertIn("media", diagnostics)
        self.assertIn("ffmpeg", diagnostics["media"])
        self.assertEqual({provider["id"] for provider in diagnostics["providers"]}, {"pexels", "pixabay"})

    def test_dashboard_ready_asset_count_matches_production_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            healthy_path = root / "healthy.mp4"
            healthy_path.write_bytes(b"healthy")
            create_asset(db, kind="media", title="Healthy", local_path=str(healthy_path), rights_status="owned_distribution", metadata={"duration": 2, "width": 720, "height": 1280})
            create_asset(db, kind="media", title="Gun clip", local_path=str(root / "flagged.mp4"), rights_status="owned_distribution", metadata={"duration": 2, "width": 720, "height": 1280})
            create_asset(db, kind="media", title="Missing", local_path=str(root / "missing.mp4"), rights_status="owned_distribution", metadata={"duration": 2, "width": 720, "height": 1280})
            counts = dashboard_counts(db)
            self.assertEqual((counts["assets"], counts["ready_assets"]), (3, 1))
            self.assertEqual([asset["title"] for asset in list_assets(db, "ready")], ["Healthy"])

    def test_text_style_preset_changes_drawtext_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            destination = root / "clean.mp4"
            source.write_bytes(b"source")
            with patch("dereel.media.available_filters", return_value={"drawtext"}), patch("dereel.media.subprocess.run", return_value=type("Result", (), {"returncode": 0, "stderr": ""})()) as runner:
                self.assertTrue(add_text_overlay(source, destination, "Title", "Subtitle", "clean"))
            command = runner.call_args.args[0]
            filter_graph = command[command.index("-vf") + 1]
            self.assertIn("fontsize=50", filter_graph)
            self.assertIn(":x=80:y=140", filter_graph)
            self.assertIn("fontsize=34", filter_graph)

    def test_pillow_overlay_fallback_builds_transparent_overlay_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            destination = root / "pillow.mp4"
            source.write_bytes(b"source")
            with patch("dereel.media.available_filters", return_value={"overlay"}), patch("dereel.media.probe", return_value={"width": 1080, "height": 1920}), patch("dereel.media.subprocess.run", return_value=type("Result", (), {"returncode": 0, "stderr": ""})()) as runner:
                self.assertEqual(text_overlay_backend(), "pillow")
                self.assertTrue(add_text_overlay(source, destination, "Title", "Subtitle", "narrated"))
            command = runner.call_args.args[0]
            self.assertIn("-filter_complex", command)
            filter_graph = command[command.index("-filter_complex") + 1]
            self.assertIn("overlay=0:0:format=auto:shortest=1", filter_graph)
            self.assertIn("-map", command)

    def test_platform_export_bounds_match_current_reel_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "instagram.mp4"
            media.write_bytes(b"export")
            metadata = {"duration": 2.0, "width": 1080, "height": 1920}
            with patch("dereel.publisher.probe", return_value=metadata):
                self.assertIn("3 second minimum", " ".join(validate_export(media, "instagram")))
            metadata["duration"] = 901.0
            with patch("dereel.publisher.probe", return_value=metadata):
                self.assertIn("900 second limit", " ".join(validate_export(media, "instagram")))
            metadata["duration"] = 30.0
            with patch("dereel.publisher.probe", return_value=metadata):
                self.assertEqual(validate_export(media, "instagram"), [])

    def test_publisher_payload_provenance_keeps_source_and_audio_ids(self) -> None:
        provenance = publish_provenance(
            {"manifest_json": json.dumps({"inputs": [{"asset_id": "asset_clip"}], "audio": {"tracks": [{"asset_id": "asset_music"}, {"asset_id": "asset_clip"}]}})},
            Path("/tmp/dereel-output"),
        )
        self.assertEqual(provenance["asset_ids"], ["asset_clip", "asset_music"])
        self.assertEqual((provenance["source_count"], provenance["audio_count"]), (1, 2))
        self.assertTrue(provenance["credits_path"].endswith("/credits.json"))

    def test_publish_jobs_bind_to_export_fingerprint_and_reject_replacement_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            for platform in ("youtube", "instagram", "tiktok"):
                (output_dir / f"{platform}.mp4").write_bytes(b"export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Fingerprint release", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(output_dir)})
            project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="export-hash"):
                jobs = create_publish_jobs(db, project, ["youtube"])
            payload = json.loads(jobs[0]["payload_json"])
            self.assertEqual(payload["media_sha256"], "export-hash")
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="replacement-hash"):
                with self.assertRaisesRegex(ValueError, "export changed"):
                    publish_job(db, {"status": "failed", "platform": "youtube", "payload_json": json.dumps({"media_path": str(output_dir / "youtube.mp4"), "media_sha256": "export-hash"})})

    def test_publish_rechecks_source_rights_after_package_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            (output_dir / "youtube.mp4").write_bytes(b"export")
            source = root / "source.mp4"
            source.write_bytes(b"source")
            db = Database(root / "test.sqlite3")
            asset = create_asset(
                db,
                kind="media",
                title="Cleared source",
                local_path=str(source),
                mime_type="video/mp4",
                rights_status="owned_distribution",
                metadata={"duration": 4, "width": 720, "height": 1280, "fingerprint": "source-hash"},
            )
            project = create_project(db, {"name": "Rights gate", "theme": "cats"})
            db.update(
                "projects",
                "id",
                project["id"],
                {
                    "status": "ready",
                    "output_dir": str(output_dir),
                    "manifest_json": json.dumps({"inputs": [{"asset_id": asset["id"]}]}),
                },
            )
            project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="export-hash"), patch("dereel.pipeline.validate_video", return_value={"duration": 4, "file_size": 6, "width": 720, "height": 1280, "has_audio": False}), patch("dereel.pipeline.sha256_file", return_value="source-hash"):
                job = create_publish_jobs(db, project, ["youtube"])[0]
            review_asset(db, asset["id"], "reject", "Rights withdrawn")
            with self.assertRaisesRegex(ValueError, "no longer cleared"):
                create_publish_jobs(db, project, ["youtube"])
            with self.assertRaisesRegex(ValueError, "no longer cleared"):
                publish_job(db, db.get("publish_jobs", "id", job["id"]) or job)

    def test_multi_platform_publish_preparation_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            (output_dir / "youtube.mp4").write_bytes(b"youtube export")
            (output_dir / "instagram.mp4").write_bytes(b"instagram export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Atomic release", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(output_dir)})
            project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", side_effect=[[], ["Instagram export is invalid"]]), patch("dereel.publisher.sha256_file", return_value="export-hash"):
                with self.assertRaisesRegex(ValueError, "Instagram Reels: Instagram export is invalid"):
                    create_publish_jobs(db, project, ["youtube", "instagram"])
            with db.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM publish_jobs WHERE project_id = ?", (project["id"],)).fetchone()[0], 0)
            self.assertFalse((output_dir / "publish-manifest.json").exists())

    def test_publish_account_routing_is_explicit_with_first_account_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            (output_dir / "youtube.mp4").write_bytes(b"youtube export")
            (output_dir / "instagram.mp4").write_bytes(b"instagram export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Routed release", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(output_dir)})
            project = db.get("projects", "id", project["id"]) or project
            for account in (
                {"id": "youtube_first", "platform": "youtube", "label": "First YouTube", "created_at": "2026-01-01"},
                {"id": "youtube_second", "platform": "youtube", "label": "Second YouTube", "created_at": "2026-01-02"},
                {"id": "instagram_main", "platform": "instagram", "label": "Main Instagram", "created_at": "2026-01-01"},
            ):
                db.insert("publishing_accounts", {**account, "status": "not_connected", "config_json": "{}", "updated_at": account["created_at"]})

            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="export-hash"):
                routed = create_publish_jobs(db, project, ["youtube", "instagram"], account_ids={"youtube": "youtube_second", "instagram": "instagram_main"})
            self.assertEqual({job["platform"]: job["account_id"] for job in routed}, {"youtube": "youtube_second", "instagram": "instagram_main"})

            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="export-hash"):
                partially_routed = create_publish_jobs(db, project, ["youtube", "instagram"], account_ids={"youtube": "youtube_first"})
            self.assertEqual({job["platform"]: job["account_id"] for job in partially_routed}, {"youtube": "youtube_first", "instagram": "instagram_main"})

            before_failed_update = {job["platform"]: job["account_id"] for job in partially_routed}
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="export-hash"):
                with self.assertRaisesRegex(ValueError, "Account does not match instagram"):
                    create_publish_jobs(db, project, ["youtube", "instagram"], account_ids={"youtube": "youtube_first", "instagram": "youtube_first"})
            with db.connection() as connection:
                after_failed_update = {row["platform"]: row["account_id"] for row in connection.execute("SELECT platform, account_id FROM publish_jobs WHERE project_id = ?", (project["id"],)).fetchall()}
            self.assertEqual(after_failed_update, before_failed_update)

            fallback_output = root / "fallback-output"
            fallback_output.mkdir()
            (fallback_output / "youtube.mp4").write_bytes(b"youtube export")
            fallback_project = create_project(db, {"name": "Fallback release", "theme": "cats"})
            db.update("projects", "id", fallback_project["id"], {"status": "ready", "output_dir": str(fallback_output)})
            fallback_project = db.get("projects", "id", fallback_project["id"]) or fallback_project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="export-hash"):
                fallback = create_publish_jobs(db, fallback_project, ["youtube"])
            self.assertEqual(fallback[0]["account_id"], "youtube_first")

    def test_publish_integrity_backfill_upgrades_legacy_outbox_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "youtube.mp4"
            media.write_bytes(b"legacy export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Legacy integrity", "theme": "cats"})
            job_id = "legacy_integrity"
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": None, "platform": "youtube", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "payload_json": json.dumps({"media_path": str(media)}), "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            self.assertEqual(backfill_publish_integrity(db), 1)
            payload = json.loads(db.get("publish_jobs", "id", job_id)["payload_json"])
            self.assertTrue(payload["media_sha256"])
            self.assertEqual(payload["provenance"]["asset_ids"], [])

    def test_publish_integrity_backfill_restores_legacy_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            media = output_dir / "youtube.mp4"
            media.write_bytes(b"legacy export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Legacy provenance", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(output_dir), "manifest_json": json.dumps({"inputs": [{"asset_id": "asset_legacy"}]})})
            job_id = "legacy_provenance"
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": None, "platform": "youtube", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "payload_json": json.dumps({"media_path": str(media)}), "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            self.assertEqual(backfill_publish_integrity(db), 1)
            payload = json.loads(db.get("publish_jobs", "id", job_id)["payload_json"])
            self.assertEqual((payload["media_sha256"], payload["provenance"]["asset_ids"]), (sha256_file(media), ["asset_legacy"]))

    def test_reused_publish_job_refreshes_provenance_and_package_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_output = root / "first"
            second_output = root / "second"
            first_output.mkdir()
            second_output.mkdir()
            (first_output / "youtube.mp4").write_bytes(b"first")
            (second_output / "youtube.mp4").write_bytes(b"second")
            old_source = root / "old.mp4"
            new_source = root / "new.mp4"
            old_source.write_bytes(b"old source")
            new_source.write_bytes(b"new source")
            db = Database(root / "test.sqlite3")
            old_asset = create_asset(db, kind="media", title="Old source", local_path=str(old_source), mime_type="video/mp4", rights_status="owned_distribution", metadata={"duration": 4, "width": 720, "height": 1280, "fingerprint": "old-source"})
            new_asset = create_asset(db, kind="media", title="New source", local_path=str(new_source), mime_type="video/mp4", rights_status="owned_distribution", metadata={"duration": 4, "width": 720, "height": 1280, "fingerprint": "new-source"})
            project = create_project(db, {"name": "Refreshing release", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(first_output), "manifest_json": json.dumps({"inputs": [{"asset_id": old_asset["id"]}]})})
            project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="first-hash"), patch("dereel.pipeline.validate_video", return_value={"duration": 4, "file_size": 10, "width": 720, "height": 1280, "has_audio": False}), patch("dereel.pipeline.sha256_file", return_value="old-source"):
                first_job = create_publish_jobs(db, project, ["youtube"])[0]
            db.update("projects", "id", project["id"], {"output_dir": str(second_output), "manifest_json": json.dumps({"inputs": [{"asset_id": new_asset["id"]}]})})
            refreshed_project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="second-hash"), patch("dereel.pipeline.validate_video", return_value={"duration": 4, "file_size": 10, "width": 720, "height": 1280, "has_audio": False}), patch("dereel.pipeline.sha256_file", return_value="new-source"):
                second_job = create_publish_jobs(db, refreshed_project, ["youtube"], scheduled_for="2026-07-16T12:00:00+08:00")[0]
            payload = json.loads(second_job["payload_json"])
            package_manifest = json.loads((second_output / "publish-manifest.json").read_text())
            self.assertEqual((second_job["id"], payload["media_sha256"], payload["provenance"]["asset_ids"]), (first_job["id"], "second-hash", [new_asset["id"]]))
            self.assertEqual(second_job["scheduled_for"], "2026-07-16T04:00:00+00:00")
            self.assertEqual((package_manifest["jobs"][0]["id"], package_manifest["jobs"][0]["provenance"]["asset_ids"], package_manifest["jobs"][0]["scheduled_for"]), (first_job["id"], [new_asset["id"]], "2026-07-16T04:00:00+00:00"))

    def test_failed_publish_job_is_reused_for_retry_with_fresh_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            (output_dir / "youtube.mp4").write_bytes(b"current export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Retry release", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(output_dir)})
            project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="first-hash"):
                original = create_publish_jobs(db, project, ["youtube"])[0]
            self.assertTrue(claim_publish_job(db, original["id"]))
            attempt = begin_publish_attempt(db, original["id"], "manual")
            fail_publish_attempt(db, original["id"], attempt["id"], "temporary upload failure")

            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="fresh-hash"):
                retried = create_publish_jobs(db, project, ["youtube"], scheduled_for="2026-07-16T12:00:00+08:00")[0]

            stored = db.get("publish_jobs", "id", original["id"]) or {}
            payload = json.loads(stored["payload_json"])
            self.assertEqual((retried["id"], stored["status"], stored["error"], stored["claim_token"]), (original["id"], "ready", "", None))
            self.assertEqual((stored["scheduled_for"], payload["media_sha256"]), ("2026-07-16T04:00:00+00:00", "fresh-hash"))
            self.assertEqual(len(list_publish_attempts(db, original["id"])), 1)

    def test_publish_preparation_cannot_mutate_a_claimed_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            (output_dir / "youtube.mp4").write_bytes(b"youtube export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Claimed release", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(output_dir)})
            project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="export-hash"):
                job = create_publish_jobs(db, project, ["youtube"])[0]
            self.assertTrue(claim_publish_job(db, job["id"]))
            before = db.get("publish_jobs", "id", job["id"]) or {}
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="replacement-hash"):
                with self.assertRaisesRegex(ValueError, "currently being processed"):
                    create_publish_jobs(db, project, ["youtube"], scheduled_for="2026-07-16T12:00:00+08:00")
            after = db.get("publish_jobs", "id", job["id"]) or {}
            self.assertEqual(
                (after["payload_json"], after["scheduled_for"], after["claim_token"]),
                (before["payload_json"], before["scheduled_for"], before["claim_token"]),
            )

    def test_publish_job_creation_rejects_invalid_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "output"
            output_dir.mkdir()
            (output_dir / "youtube.mp4").write_bytes(b"export")
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Invalid schedule", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(output_dir)})
            with patch("dereel.publisher.validate_export", return_value=[]):
                with self.assertRaisesRegex(ValueError, "Invalid scheduled_for"):
                    create_publish_jobs(db, db.get("projects", "id", project["id"]) or project, ["youtube"], scheduled_for="not-a-date")

    def test_publish_failure_fallback_closes_attempt_and_releases_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Fallback", "theme": "cats"})
            job_id = "publish_failure_fallback"
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": None, "platform": "youtube", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            self.assertTrue(claim_publish_job(db, job_id))
            attempt = begin_publish_attempt(db, job_id, "manual")
            failed = fail_publish_attempt(db, job_id, attempt["id"], "adapter crashed")
            self.assertEqual((failed["status"], failed["claim_token"], failed["error"]), ("failed", None, "adapter crashed"))
            self.assertEqual(list_publish_attempts(db, job_id)[0]["status"], "failed")

    def test_publish_success_fallback_records_remote_id_and_releases_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Success fallback", "theme": "cats"})
            job_id = "publish_success_fallback"
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": None, "platform": "youtube", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            self.assertTrue(claim_publish_job(db, job_id))
            attempt = begin_publish_attempt(db, job_id, "manual")
            published = force_publish_success(db, job_id, attempt["id"], "remote-123")
            self.assertEqual((published["status"], published["remote_id"], published["claim_token"]), ("published", "remote-123", None))
            self.assertEqual(list_publish_attempts(db, job_id)[0]["status"], "published")

    def test_publish_success_finalizer_uses_fallback_after_completion_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Finalizer", "theme": "cats"})
            job_id = "publish_success_finalizer"
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": None, "platform": "youtube", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            self.assertTrue(claim_publish_job(db, job_id))
            attempt = begin_publish_attempt(db, job_id, "manual")
            with patch("dereel.server.complete_publish_attempt", side_effect=ValueError("state race")):
                finalize_publish_success(db, job_id, attempt, "remote-finalizer")
            job = db.get("publish_jobs", "id", job_id)
            self.assertEqual((job["status"], job["remote_id"], job["claim_token"]), ("published", "remote-finalizer", None))
            self.assertEqual(list_publish_attempts(db, job_id)[0]["status"], "published")

    def test_tiktok_draft_handoff_is_distinct_until_marked_posted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "TikTok draft", "theme": "cats"})
            job_id = "tiktok_draft_state"
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": None, "platform": "tiktok", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "cancelled_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            self.assertTrue(claim_publish_job(db, job_id))
            attempt = begin_publish_attempt(db, job_id, "manual")
            self.assertEqual(publish_result_status("draft:publish_1"), "drafted")
            finalize_publish_success(db, job_id, attempt, "draft:publish_1")
            self.assertEqual(db.get("publish_jobs", "id", job_id)["status"], "drafted")
            self.assertEqual(list_publish_attempts(db, job_id)[0]["status"], "drafted")
            marked = mark_publish_job(db, job_id, "tiktok-posted")
            self.assertEqual((marked["status"], marked["remote_id"]), ("published", "tiktok-posted"))
            self.assertEqual([item["status"] for item in list_publish_attempts(db, job_id)], ["published", "drafted"])

    def test_published_jobs_cannot_be_replayed(self) -> None:
        with self.assertRaisesRegex(ValueError, "already marked published"):
            publish_job(None, {"status": "published"})

    def test_publish_claim_is_atomic_and_restart_releases_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Publish claim", "theme": "claim"})
            job_id = "publish_claim_test"
            db.insert(
                "publish_jobs",
                {
                    "id": job_id,
                    "project_id": project["id"],
                    "account_id": None,
                    "platform": "youtube",
                    "status": "ready",
                    "scheduled_for": "",
                    "remote_id": "",
                    "error": "",
                    "claim_token": None,
                    "claimed_at": None,
                    "payload_json": "{}",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            )
            self.assertTrue(claim_publish_job(db, job_id))
            self.assertFalse(claim_publish_job(db, job_id))
            self.assertTrue(publish_job_view(db.get("publish_jobs", "id", job_id) or {})["processing"])
            recovered = recover_interrupted_jobs(db)
            self.assertEqual(recovered, [])
            stored = db.get("publish_jobs", "id", job_id)
            self.assertEqual((stored["status"], stored["error"], stored["claim_token"], stored["claimed_at"]), ("failed", "Interrupted by process restart", None, None))
            self.assertTrue(claim_publish_job(db, job_id))
            complete_publish_job(db, job_id, "failed", error="Still unavailable")
            marked = mark_publish_job(db, job_id, "manual-1")
            self.assertEqual((marked["status"], marked["remote_id"]), ("published", "manual-1"))
            self.assertEqual([(item["trigger"], item["status"], item["remote_id"]) for item in list_publish_attempts(db, job_id)], [("manual", "published", "manual-1")])
            with self.assertRaisesRegex(ValueError, "already published"):
                mark_publish_job(db, job_id)

    def test_publish_revalidates_the_export_at_attempt_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            with self.assertRaisesRegex(ValueError, "export is missing"):
                publish_job(
                    db,
                    {
                        "status": "failed",
                        "platform": "instagram",
                        "account_id": None,
                        "payload_json": json.dumps({"media_path": str(Path(directory) / "missing.mp4")}),
                    },
                )

    def test_publish_attempt_history_is_numbered_and_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Attempt history", "theme": "history"})
            job_id = "publish_attempt_history"
            db.insert(
                "publish_jobs",
                {
                    "id": job_id,
                    "project_id": project["id"],
                    "account_id": None,
                    "platform": "youtube",
                    "status": "ready",
                    "scheduled_for": "",
                    "remote_id": "",
                    "error": "",
                    "claim_token": None,
                    "claimed_at": None,
                    "payload_json": "{}",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            )
            self.assertTrue(claim_publish_job(db, job_id))
            first = begin_publish_attempt(db, job_id, "manual")
            complete_publish_attempt(db, first["id"], "failed", error="Token unavailable")
            self.assertTrue(claim_publish_job(db, job_id))
            second = begin_publish_attempt(db, job_id, "scheduler")
            recovered = recover_interrupted_jobs(db)
            self.assertEqual(recovered, [])
            attempts = list_publish_attempts(db, job_id)
            self.assertEqual([(item["attempt_number"], item["trigger"], item["status"]) for item in attempts], [(2, "scheduler", "failed"), (1, "manual", "failed")])
            self.assertEqual(attempts[0]["error"], "Interrupted by process restart")
            stored_job = db.get("publish_jobs", "id", job_id)
            self.assertEqual((stored_job["status"], stored_job["claim_token"]), ("failed", None))
            with self.assertRaisesRegex(ValueError, "not running"):
                complete_publish_attempt(db, second["id"], "failed", error="duplicate finalization")

    def test_publish_attempt_completion_updates_job_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Atomic publish", "theme": "atomic"})
            job_id = "publish_attempt_atomic"
            db.insert(
                "publish_jobs",
                {
                    "id": job_id,
                    "project_id": project["id"],
                    "account_id": None,
                    "platform": "tiktok",
                    "status": "ready",
                    "scheduled_for": "",
                    "remote_id": "",
                    "error": "",
                    "claim_token": None,
                    "claimed_at": None,
                    "payload_json": "{}",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                },
            )
            self.assertTrue(claim_publish_job(db, job_id))
            attempt = begin_publish_attempt(db, job_id, "manual")
            complete_publish_attempt(db, attempt["id"], "published", remote_id="draft:123")
            self.assertEqual(list_publish_attempts(db, job_id)[0]["remote_id"], "draft:123")
            self.assertEqual((db.get("publish_jobs", "id", job_id)["status"], db.get("publish_jobs", "id", job_id)["remote_id"]), ("published", "draft:123"))

    def test_watch_import_rejects_source_changes_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watch_dir = root / "watch"
            watch_dir.mkdir()
            source = watch_dir / "changing.mp4"
            source.write_bytes(b"source")
            db = Database(root / "test.sqlite3")
            automation = create_automation(db, {"name": "Changing source", "watch_dir": str(watch_dir)})

            def changed_copy(_source: Path, destination: Path, **_kwargs: Any) -> int:
                destination.write_bytes(b"replacement")
                return destination.stat().st_size

            metadata = {"duration": 2, "file_size": 6, "width": 720, "height": 1280, "has_audio": False}
            with patch("dereel.automation.validate_video", return_value=metadata), patch("dereel.automation.atomic_copy_file", side_effect=changed_copy):
                result = scan_watch_folder(db, automation, root / "library")
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("changed during import", result["errors"][0]["error"])
            with db.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0)
            self.assertFalse(list((root / "library").glob("*")))

    def test_youtube_upload_sends_resumable_byte_range(self) -> None:
        class FakeResponse:
            def __init__(self, body=b"{}", headers=None):
                self._body, self.headers = body, headers or {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return self._body

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "youtube.mp4"
            media.write_bytes(b"0123456789")
            requests = []

            def fake_urlopen(request, timeout):
                requests.append(request)
                if len(requests) == 1:
                    return FakeResponse(headers={"Location": "https://upload.example/video"})
                return FakeResponse(b'{"id":"video_1"}')

            with patch("dereel.publisher.urllib.request.urlopen", side_effect=fake_urlopen):
                result = publish_youtube({"media_path": str(media)}, "token")
            self.assertEqual(result, "video_1")
            self.assertEqual(requests[1].data, b"0123456789")
            self.assertEqual(requests[1].get_header("Content-length"), "10")
            self.assertEqual(requests[1].get_header("Content-range"), "bytes 0-9/10")

    def test_youtube_upload_resumes_after_transient_failure(self) -> None:
        class FakeResponse:
            def __init__(self, body=b"{}", status=200, headers=None):
                self._body, self.status, self.headers = body, status, headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self._body

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "youtube.mp4"
            media.write_bytes(b"0123456789")
            requests = []
            transient = urllib_error.HTTPError("https://upload.example/video", 503, "busy", {}, io.BytesIO(b"busy"))

            def fake_urlopen(request, timeout):
                requests.append(request)
                if len(requests) == 1:
                    return FakeResponse(headers={"Location": "https://upload.example/video"})
                if len(requests) == 2:
                    raise transient
                if len(requests) == 3:
                    return FakeResponse(status=308, headers={"Range": "bytes=0-4"})
                return FakeResponse(b'{"id":"video_resumed"}')

            with patch("dereel.publisher.urllib.request.urlopen", side_effect=fake_urlopen):
                with patch("dereel.publisher.time.sleep") as sleep:
                    result = publish_youtube({"media_path": str(media)}, "token")
            transient.close()

            self.assertEqual(result, "video_resumed")
            self.assertEqual(requests[2].get_header("Content-range"), "bytes */10")
            self.assertEqual(requests[3].get_header("Content-range"), "bytes 5-9/10")
            sleep.assert_not_called()

    def test_tiktok_upload_returns_draft_id_and_uses_byte_ranges(self) -> None:
        class FakeResponse:
            def __init__(self, body: bytes = b"{}", status: int = 200, headers: dict[str, str] | None = None) -> None:
                self._body = body
                self.status = status
                self.headers = headers or {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return self._body

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "tiktok.mp4"
            media.write_bytes(b"0123456789")
            requests = []

            def fake_urlopen(request, timeout):
                requests.append((request, timeout))
                if len(requests) == 1:
                    return FakeResponse(b'{"data":{"publish_id":"publish_1","upload_url":"https://upload.example/video"},"error":{"code":"ok"}}')
                return FakeResponse(status=201)

            with patch("dereel.publisher.urllib.request.urlopen", side_effect=fake_urlopen):
                result = publish_tiktok_draft({"media_path": str(media)}, "token")

            self.assertEqual(result, "draft:publish_1")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0][0].full_url, "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/")
            self.assertEqual(requests[1][0].get_header("Content-range"), "bytes 0-9/10")
            self.assertEqual(requests[1][0].data, b"0123456789")

    def test_tiktok_upload_retries_only_the_ranged_transfer(self) -> None:
        class FakeResponse:
            def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
                self._body = body
                self.status = status
                self.headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return self._body

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "tiktok.mp4"
            media.write_bytes(b"0123456789")
            requests = []

            def fake_urlopen(request, timeout):
                requests.append(request)
                if len(requests) == 1:
                    return FakeResponse(b'{"data":{"publish_id":"publish_1","upload_url":"https://upload.example/video"},"error":{"code":"ok"}}')
                if len(requests) == 2:
                    raise urllib_error.URLError("temporary network failure")
                return FakeResponse(status=201)

            with patch("dereel.publisher.urllib.request.urlopen", side_effect=fake_urlopen):
                with patch("dereel.publisher.time.sleep") as sleep:
                    result = publish_tiktok_draft({"media_path": str(media)}, "token")

            self.assertEqual(result, "draft:publish_1")
            self.assertEqual(len(requests), 3)
            self.assertEqual(requests[0].full_url, "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/")
            self.assertEqual(requests[1].get_header("Content-range"), "bytes 0-9/10")
            self.assertEqual(requests[2].get_header("Content-range"), "bytes 0-9/10")
            sleep.assert_called_once()

    def test_instagram_reel_uses_container_poll_and_publish_flow(self) -> None:
        class FakeResponse:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return self._body

        responses = [
            FakeResponse(b'{"id":"container_1"}'),
            FakeResponse(b'{"status_code":"FINISHED"}'),
            FakeResponse(b'{"id":"media_1"}'),
        ]
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return responses.pop(0)

        account = {"config_json": '{"instagram_user_id":"ig_123","public_media_base_url":"https://media.example"}'}
        payload = {"project_id": "project_1", "platform": "instagram", "caption": "A caption"}
        with patch("dereel.publisher.urllib.request.urlopen", side_effect=fake_urlopen):
            with patch("dereel.publisher.time.monotonic", return_value=0):
                result = publish_instagram_reel(payload, account, "token")

        self.assertEqual(result, "media_1")
        self.assertEqual(len(requests), 3)
        container_form = parse_qs(requests[0][0].data.decode())
        self.assertEqual(container_form["media_type"], ["REELS"])
        self.assertEqual(container_form["video_url"], ["https://media.example/api/projects/project_1/output/instagram.mp4"])
        self.assertEqual(parse_qs(requests[1][0].full_url.split("?", 1)[1])["fields"], ["status_code,status"])
        self.assertIn("creation_id=container_1", requests[2][0].data.decode())

    def test_project_clip_trim_and_enable_state_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            asset = create_asset(db, kind="media", title="Clip", rights_status="owned_distribution", metadata={"duration": 10, "width": 720, "height": 1280})
            project = create_project(db, {"name": "Trim check", "theme": "sample"})
            with db.connection() as connection:
                connection.execute("INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, enabled, selected_by) VALUES (?, ?, 0, 0, 10, 1, 'test')", (project["id"], asset["id"]))
            updated = update_project_asset(db, project["id"], asset["id"], {"in_point": 2, "out_point": 6, "enabled": 0})
            self.assertEqual((updated["in_point"], updated["out_point"], updated["enabled"]), (2.0, 6.0, False))
            with self.assertRaises(ValueError):
                update_project_asset(db, project["id"], asset["id"], {"in_point": 7, "out_point": 4})

    def test_planner_never_selects_audio_assets_as_clips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            video_path = root / "video.mp4"
            video_path.write_bytes(b"video")
            video = create_asset(db, kind="media", title="Funny video", local_path=str(video_path), mime_type="video/mp4", tags=["funny"], rights_status="owned_distribution", metadata={"duration": 5, "width": 720, "height": 1280})
            create_asset(db, kind="media", title="Funny music", local_path="/tmp/music.m4a", mime_type="audio/mp4", tags=["funny", "music"], rights_status="owned_distribution", metadata={"duration": 10, "has_audio": True})
            project = create_project(db, {"name": "Planner check", "theme": "funny music", "duration_target": 5})
            planned = plan_project(db, project["id"])
            self.assertEqual([clip["asset_id"] for clip in planned["assets"]], [video["id"]])
            self.assertIn("score", planned["assets"][0]["note"])
            self.assertIn("theme", planned["manifest"]["selected"][0]["reason"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_render_creates_vertical_outputs_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=0x31565a:s=320x568:d=1.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
                    "-t", "1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
                ],
                check=True,
            )
            db = Database(root / "test.sqlite3")
            metadata = validate_video(source)
            metadata["fingerprint"] = sha256_file(source)
            asset = create_asset(db, kind="media", title="Sample", tags=["sample"], local_path=str(source), rights_status="owned_distribution", metadata=metadata)
            project = create_project(db, {"name": "Render check", "theme": "sample", "duration_target": 8, "caption": "Sample caption"})
            with db.connection() as connection:
                connection.execute("INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, enabled, selected_by) VALUES (?, ?, 0, 0, 1.5, 1, 'test')", (project["id"], asset["id"]))
            result = render_project(db, project["id"], root / "outputs", lambda _progress, _message: None)
            output = Path(result["output_dir"]) / "final.mp4"
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual(result["manifest"]["outputs"]["master"], str(output))
            self.assertEqual(result["manifest"]["validation"]["status"], "passed")
            self.assertEqual(set(result["manifest"]["validation"]["outputs"]), {"master", "youtube", "instagram", "tiktok"})
            self.assertEqual(result["manifest"]["inputs"][0]["provenance"]["source_type"], "local_import")
            self.assertIn(result["manifest"]["title_treatment"]["mode"], {"drawtext", "pillow", "unavailable"})
            self.assertEqual(result["manifest"]["title_treatment"]["applied"], result["manifest"]["title_treatment"]["mode"] in {"drawtext", "pillow"})
            self.assertEqual(result["manifest"]["captions"]["mode"], "static_project_caption")
            validated = validate_project_output(db, project["id"])
            self.assertEqual(validated["status"], "ready")
            self.assertEqual(validated["manifest"]["validation"]["status"], "passed")
            volume_check = subprocess.run(
                ["ffmpeg", "-hide_banner", "-i", str(output), "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True,
                text=True,
                check=True,
            )
            volume_match = re.search(r"mean_volume:\s+(-?\d+(?:\.\d+)?) dB", volume_check.stderr)
            self.assertIsNotNone(volume_match)
            self.assertGreater(float(volume_match.group(1)), -50)
            credits = json.loads((Path(result["output_dir"]) / "credits.json").read_text())
            self.assertEqual(credits["assets"], [asset["id"]])
            self.assertEqual(credits["provenance"][0]["fingerprint"], asset["fingerprint"])
            captions = Path(result["output_dir"]) / "captions.srt"
            self.assertTrue(captions.is_file())
            self.assertIn("1\n00:00:00,000", captions.read_text())

    def test_render_output_validation_reports_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = validate_render_outputs(Path(directory))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(set(report["outputs"]), {"master", "youtube", "instagram", "tiktok"})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_render_mixes_a_cleared_audio_bed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            audio = root / "audio.m4a"
            sfx = root / "sfx.m4a"
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x31565a:s=320x568:d=1.5", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=1.5", "-c:a", "aac", str(audio)], check=True)
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.3", "-c:a", "aac", str(sfx)], check=True)
            db = Database(root / "test.sqlite3")
            video_metadata = validate_video(source)
            audio_metadata = validate_audio(audio)
            sfx_metadata = validate_audio(sfx)
            video = create_asset(db, kind="media", title="Sample video", local_path=str(source), rights_status="owned_distribution", metadata=video_metadata)
            music = create_asset(db, kind="media", title="Sample bed", local_path=str(audio), mime_type="audio/mp4", rights_status="owned_distribution", metadata=audio_metadata)
            effect = create_asset(db, kind="media", title="Sample effect", local_path=str(sfx), mime_type="audio/mp4", rights_status="owned_distribution", metadata=sfx_metadata)
            project = create_project(db, {"name": "Audio check", "theme": "sample", "duration_target": 8, "audio_tracks": [{"asset_id": music["id"], "role": "music", "volume": .12}, {"asset_id": effect["id"], "role": "sfx", "volume": .7, "start_time": .4}]})
            self.assertEqual([track["role"] for track in project["audio"]], ["music", "sfx"])
            with db.connection() as connection:
                connection.execute("INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, enabled, selected_by) VALUES (?, ?, 0, 0, 1.5, 1, 'test')", (project["id"], video["id"]))
            result = render_project(db, project["id"], root / "outputs", lambda _progress, _message: None)
            self.assertEqual([track["role"] for track in result["manifest"]["audio"]["tracks"]], ["music", "sfx"])
            self.assertTrue((Path(result["output_dir"]) / "final.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
