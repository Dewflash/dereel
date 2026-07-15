from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from dereel.db import Database
from dereel.media import sha256_file, validate_audio, validate_video
from dereel.pipeline import add_project_asset, audit_asset_health, bulk_review_assets, check_asset_health, create_asset, create_job, create_project, delete_project, get_project_assets, list_asset_reviews, move_project_asset, plan_project, remove_project_asset, render_project, retry_failed_job, review_asset, score_asset, score_breakdown, set_job, update_project, update_project_asset, validate_project_output, validate_render_outputs
from dereel.scout import _host_allowed
from dereel.automation import claim_automation, create_automation, create_automation_run, due_automations, finish_automation, finish_automation_run, list_automation_runs, run_automation, scan_provider_sources, scan_watch_folder, update_automation
from dereel.publisher import account_view, backfill_publish_integrity, begin_publish_attempt, cancel_publish_job, claim_publish_job, complete_publish_attempt, complete_publish_job, create_publish_jobs, delete_publishing_account, fail_publish_attempt, force_publish_success, list_publish_attempts, mark_publish_job, platform_capabilities, publish_instagram_reel, publish_job, publish_job_view, publish_provenance, publish_tiktok_draft, reschedule_publish_job, update_publishing_account
from dereel.config import Settings
from dereel.server import AppState, finalize_publish_success, recover_interrupted_jobs, runtime_diagnostics


class CoreModelTests(unittest.TestCase):
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
                review_columns = {row[1] for row in connection.execute("PRAGMA table_info(asset_reviews)").fetchall()}
                schema_version = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
            self.assertIn("automation_id", columns)
            self.assertIn("retry_of", columns)
            self.assertIn("attempt", columns)
            self.assertIn("claim_token", publish_columns)
            self.assertIn("claimed_at", publish_columns)
            self.assertEqual(schema_version, "10")
            self.assertIn("attempt_number", attempt_columns)
            self.assertIn("cancelled_at", publish_columns)
            self.assertIn("previous_rights_status", review_columns)

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
            self.assertEqual(schema_version, "10")

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

    def test_active_automation_freezes_configuration_but_allows_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            automation = create_automation(db, {"name": "Active recipe", "watch_dir": directory})
            self.assertTrue(claim_automation(db, automation["id"]))
            with self.assertRaisesRegex(ValueError, "cannot change while it is processing"):
                update_automation(db, automation["id"], {"name": "Changed while active"})
            paused = update_automation(db, automation["id"], {"enabled": False})
            self.assertFalse(paused["enabled"])

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

    def test_project_text_treatment_settings_normalize_boolean_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "No treatment", "theme": "quiet", "add_title_card": "false", "write_captions": 0})
            self.assertEqual((project["settings"]["add_title_card"], project["settings"]["write_captions"]), (False, False))

    def test_project_settings_update_replaces_audio_and_invalidates_old_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
            music = create_asset(db, kind="media", title="Music", local_path=str(root / "music.m4a"), mime_type="audio/mp4", rights_status="owned_distribution", metadata={"duration": 8, "has_audio": True})
            project = create_project(db, {"name": "Before", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(root / "old-output"), "manifest_json": json.dumps({"validation": {"status": "passed"}})})
            updated = update_project(db, project["id"], {"name": "After", "theme": "cats funny", "title": "Cats in trouble", "caption": "Perfect timing.", "duration_target": 42, "style": "clean", "add_title_card": False, "write_captions": True, "audio_tracks": [{"asset_id": music["id"], "role": "music", "volume": 0.2}]})
            stored = db.get("projects", "id", project["id"])
            self.assertEqual((updated["name"], updated["duration_target"], updated["style"]), ("After", 42.0, "clean"))
            self.assertEqual((stored["status"], stored["output_dir"], stored["manifest_json"]), ("draft", "", "{}"))
            self.assertEqual([(track["asset_id"], track["role"], track["volume"]) for track in updated["audio"]], [(music["id"], "music", 0.2)])
            self.assertFalse(updated["settings"]["add_title_card"])

    def test_publishing_requires_a_fresh_ready_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Draft release", "theme": "cats"})
            with self.assertRaisesRegex(ValueError, "must be rendered and ready"):
                create_publish_jobs(db, project, ["youtube"])

    def test_publisher_account_and_outbox_lifecycle_is_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            account_id = "account_lifecycle"
            project = create_project(db, {"name": "Release", "theme": "cats"})
            db.insert("publishing_accounts", {"id": account_id, "platform": "instagram", "label": "Old label", "status": "not_connected", "config_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            updated_account = update_publishing_account(db, account_id, {"label": "Main Reels", "instagram_user_id": "ig_123", "public_media_base_url": "https://media.example"})
            self.assertEqual((updated_account["label"], updated_account["config"]["instagram_user_id"]), ("Main Reels", "ig_123"))
            job_id = "outbox_lifecycle"
            db.insert("publish_jobs", {"id": job_id, "project_id": project["id"], "account_id": account_id, "platform": "instagram", "status": "ready", "scheduled_for": "", "remote_id": "", "error": "", "claim_token": None, "claimed_at": None, "payload_json": "{}", "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"})
            with self.assertRaisesRegex(ValueError, "pending publish job"):
                delete_publishing_account(db, account_id)
            scheduled = reschedule_publish_job(db, job_id, "2026-02-01T12:00:00+00:00")
            self.assertEqual(scheduled["scheduled_for"], "2026-02-01T12:00:00+00:00")
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

    def test_publisher_capabilities_are_explicit(self) -> None:
        capabilities = {item["id"]: item for item in platform_capabilities()}
        self.assertEqual(capabilities["youtube"]["mode"], "direct_upload")
        self.assertEqual(capabilities["instagram"]["mode"], "public_url_container")
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

    def test_runtime_diagnostics_reports_media_and_provider_checks(self) -> None:
        diagnostics = runtime_diagnostics()
        self.assertIn("media", diagnostics)
        self.assertIn("ffmpeg", diagnostics["media"])
        self.assertEqual({provider["id"] for provider in diagnostics["providers"]}, {"pexels", "pixabay"})

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
            db = Database(root / "test.sqlite3")
            project = create_project(db, {"name": "Refreshing release", "theme": "cats"})
            db.update("projects", "id", project["id"], {"status": "ready", "output_dir": str(first_output), "manifest_json": json.dumps({"inputs": [{"asset_id": "asset_old"}]})})
            project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="first-hash"):
                first_job = create_publish_jobs(db, project, ["youtube"])[0]
            db.update("projects", "id", project["id"], {"output_dir": str(second_output), "manifest_json": json.dumps({"inputs": [{"asset_id": "asset_new"}]})})
            refreshed_project = db.get("projects", "id", project["id"]) or project
            with patch("dereel.publisher.validate_export", return_value=[]), patch("dereel.publisher.sha256_file", return_value="second-hash"):
                second_job = create_publish_jobs(db, refreshed_project, ["youtube"])[0]
            payload = json.loads(second_job["payload_json"])
            package_manifest = json.loads((second_output / "publish-manifest.json").read_text())
            self.assertEqual((second_job["id"], payload["media_sha256"], payload["provenance"]["asset_ids"]), (first_job["id"], "second-hash", ["asset_new"]))
            self.assertEqual((package_manifest["jobs"][0]["id"], package_manifest["jobs"][0]["provenance"]["asset_ids"]), (first_job["id"], ["asset_new"]))

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
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
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
            self.assertIn(result["manifest"]["title_treatment"]["mode"], {"drawtext", "unavailable"})
            self.assertEqual(result["manifest"]["title_treatment"]["applied"], result["manifest"]["title_treatment"]["mode"] == "drawtext")
            self.assertEqual(result["manifest"]["captions"]["mode"], "static_project_caption")
            validated = validate_project_output(db, project["id"])
            self.assertEqual(validated["status"], "ready")
            self.assertEqual(validated["manifest"]["validation"]["status"], "passed")
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
