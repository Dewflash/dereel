from __future__ import annotations

import json
import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from dereel.db import Database
from dereel.media import sha256_file, validate_audio, validate_video
from dereel.pipeline import bulk_review_assets, create_asset, create_job, create_project, plan_project, render_project, score_asset, score_breakdown, set_job, update_project_asset
from dereel.scout import _host_allowed
from dereel.automation import claim_automation, create_automation, due_automations, finish_automation, run_automation, scan_provider_sources
from dereel.publisher import account_view, platform_capabilities, publish_instagram_reel, publish_job, publish_provenance, publish_tiktok_draft
from dereel.config import Settings
from dereel.server import AppState, recover_interrupted_jobs, runtime_diagnostics


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
            rejected = create_asset(db, kind="media", title="Rejected", local_path="/tmp/rejected.mp4", rights_status="pending_review")
            self.assertEqual(bulk_review_assets(db, [rejected["id"]], "reject")[0]["rights_status"], "blocked")
            pending = create_asset(db, kind="media", title="Pending", local_path="/tmp/pending.mp4", rights_status="pending_review")
            reference = create_asset(db, kind="reference", title="Reference", source_url="https://example.com")
            with self.assertRaisesRegex(ValueError, "awaiting review"):
                bulk_review_assets(db, [pending["id"], reference["id"]], "approve_owned")
            self.assertEqual(db.get("assets", "id", pending["id"])["rights_status"], "pending_review")

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

    def test_automation_audio_controls_persist_and_feed_new_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.sqlite3")
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
            recovered = recover_interrupted_jobs(db)
            self.assertEqual(recovered, [job["id"]])
            stored_job = db.get("jobs", "id", job["id"])
            self.assertEqual((stored_job["status"], stored_job["error"]), ("failed", "Interrupted by process restart"))
            self.assertEqual(db.get("projects", "id", project["id"])["status"], "failed")

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

    def test_published_jobs_cannot_be_replayed(self) -> None:
        with self.assertRaisesRegex(ValueError, "already marked published"):
            publish_job(None, {"status": "published"})

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
            db = Database(Path(directory) / "test.sqlite3")
            video = create_asset(db, kind="media", title="Funny video", local_path="/tmp/video.mp4", mime_type="video/mp4", tags=["funny"], rights_status="owned_distribution", metadata={"duration": 5, "width": 720, "height": 1280})
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
            self.assertEqual(result["manifest"]["inputs"][0]["provenance"]["source_type"], "local_import")
            self.assertIn(result["manifest"]["title_treatment"]["mode"], {"drawtext", "unavailable"})
            self.assertEqual(result["manifest"]["title_treatment"]["applied"], result["manifest"]["title_treatment"]["mode"] == "drawtext")
            self.assertEqual(result["manifest"]["captions"]["mode"], "static_project_caption")
            credits = json.loads((Path(result["output_dir"]) / "credits.json").read_text())
            self.assertEqual(credits["assets"], [asset["id"]])
            self.assertEqual(credits["provenance"][0]["fingerprint"], asset["fingerprint"])
            captions = Path(result["output_dir"]) / "captions.srt"
            self.assertTrue(captions.is_file())
            self.assertIn("1\n00:00:00,000", captions.read_text())

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
