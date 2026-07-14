from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .db import Database, json_dumps, new_id, now_iso
from .media import probe


PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "youtube": {"label": "YouTube Shorts", "max_seconds": 180, "requires_public_url": False},
    "instagram": {"label": "Instagram Reels", "max_seconds": 90, "requires_public_url": True},
    "tiktok": {"label": "TikTok", "max_seconds": 180, "requires_public_url": False},
}


def validate_export(path: Path, platform: str) -> list[str]:
    if platform not in PLATFORM_SPECS:
        return ["Unsupported platform"]
    if not path.is_file():
        return ["Rendered export is missing"]
    metadata = probe(path)
    problems: list[str] = []
    if metadata["duration"] <= 0:
        problems.append("Export has no duration")
    if metadata["duration"] > PLATFORM_SPECS[platform]["max_seconds"]:
        problems.append(f"Export is longer than the {PLATFORM_SPECS[platform]['max_seconds']} second limit")
    if metadata["width"] != 1080 or metadata["height"] != 1920:
        problems.append("Export is not 1080x1920 vertical")
    return problems


def create_publish_jobs(
    db: Database,
    project: dict[str, Any],
    platforms: list[str],
    scheduled_for: str | None = None,
) -> list[dict[str, Any]]:
    output_dir = Path(project.get("output_dir") or "")
    if not output_dir.is_dir():
        raise ValueError("Render the project before creating publish jobs")
    jobs: list[dict[str, Any]] = []
    for platform in platforms:
        if platform not in PLATFORM_SPECS:
            raise ValueError(f"Unsupported platform: {platform}")
        export = output_dir / f"{platform}.mp4"
        problems = validate_export(export, platform)
        if problems:
            raise ValueError(f"{PLATFORM_SPECS[platform]['label']}: " + "; ".join(problems))
        job_id = new_id("publish")
        now = now_iso()
        payload = {
            "platform": platform,
            "title": project.get("title") or project.get("name"),
            "caption": project.get("caption", ""),
            "media_path": str(export),
            "requires_public_url": PLATFORM_SPECS[platform]["requires_public_url"],
            "state": "awaiting_account_connection",
        }
        db.insert(
            "publish_jobs",
            {
                "id": job_id,
                "project_id": project["id"],
                "account_id": None,
                "platform": platform,
                "status": "ready",
                "scheduled_for": scheduled_for or "",
                "remote_id": "",
                "error": "",
                "payload_json": json_dumps(payload),
                "created_at": now,
                "updated_at": now,
            },
        )
        jobs.append(db.get("publish_jobs", "id", job_id) or {})
        manifest_path = output_dir / "publish-manifest.json"
        existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"jobs": []}
        existing.setdefault("jobs", []).append({"id": job_id, **payload, "scheduled_for": scheduled_for})
        manifest_path.write_text(json.dumps(existing, indent=2))
    return jobs


def publish_job_view(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("payload_json", "{}"))
    except json.JSONDecodeError:
        item["payload"] = {}
    return item
