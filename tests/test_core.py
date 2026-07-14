from __future__ import annotations

import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path

from dereel.db import Database
from dereel.media import sha256_file, validate_audio, validate_video
from dereel.pipeline import create_asset, create_project, render_project, score_asset, update_project_asset
from dereel.scout import _host_allowed
from dereel.publisher import account_view, platform_capabilities


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

    def test_project_defaults_to_vertical_edit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            project = create_project(db, {"name": "Cat cuts", "theme": "cats funny"})
            self.assertEqual(project["format"], "vertical_9_16")
            self.assertEqual(project["status"], "draft")
            self.assertEqual(project["assets"], [])

    def test_score_prefers_theme_matches_and_short_clips(self) -> None:
        matching = {"title": "cat surprise", "tags_json": '["cats","funny"]', "duration": 6, "width": 1080, "height": 1920, "has_audio": 1}
        weak = {"title": "landscape", "tags_json": "[]", "duration": 45, "width": 1920, "height": 1080, "has_audio": 0}
        self.assertGreater(score_asset(matching, {"cat", "funny"}), score_asset(weak, {"cat", "funny"}))

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
            project = create_project(db, {"name": "Render check", "theme": "sample", "duration_target": 8})
            with db.connection() as connection:
                connection.execute("INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, enabled, selected_by) VALUES (?, ?, 0, 0, 1.5, 1, 'test')", (project["id"], asset["id"]))
            result = render_project(db, project["id"], root / "outputs", lambda _progress, _message: None)
            output = Path(result["output_dir"]) / "final.mp4"
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual(result["manifest"]["outputs"]["master"], str(output))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_render_mixes_a_cleared_audio_bed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            audio = root / "audio.m4a"
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x31565a:s=320x568:d=1.5", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True)
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=1.5", "-c:a", "aac", str(audio)], check=True)
            db = Database(root / "test.sqlite3")
            video_metadata = validate_video(source)
            audio_metadata = validate_audio(audio)
            video = create_asset(db, kind="media", title="Sample video", local_path=str(source), rights_status="owned_distribution", metadata=video_metadata)
            music = create_asset(db, kind="media", title="Sample bed", local_path=str(audio), mime_type="audio/mp4", rights_status="owned_distribution", metadata=audio_metadata)
            project = create_project(db, {"name": "Audio check", "theme": "sample", "duration_target": 8, "music_asset_id": music["id"]})
            with db.connection() as connection:
                connection.execute("INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, enabled, selected_by) VALUES (?, ?, 0, 0, 1.5, 1, 'test')", (project["id"], video["id"]))
            result = render_project(db, project["id"], root / "outputs", lambda _progress, _message: None)
            self.assertEqual(result["manifest"]["audio"]["asset_id"], music["id"])
            self.assertTrue((Path(result["output_dir"]) / "final.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
