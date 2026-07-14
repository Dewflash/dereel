from __future__ import annotations

import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path

from dereel.db import Database
from dereel.media import sha256_file, validate_video
from dereel.pipeline import create_asset, create_project, render_project, score_asset


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


if __name__ == "__main__":
    unittest.main()
