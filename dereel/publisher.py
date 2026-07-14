from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .db import Database, json_dumps, new_id, now_iso
from .media import probe


PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "youtube": {"label": "YouTube Shorts", "max_seconds": 180, "requires_public_url": False},
    "instagram": {"label": "Instagram Reels", "max_seconds": 90, "requires_public_url": True},
    "tiktok": {"label": "TikTok", "max_seconds": 180, "requires_public_url": False},
}

PLATFORM_ENV = {
    "youtube": "DEREEL_YOUTUBE_ACCESS_TOKEN",
    "instagram": "DEREEL_INSTAGRAM_ACCESS_TOKEN",
    "tiktok": "DEREEL_TIKTOK_ACCESS_TOKEN",
}


def platform_capabilities() -> list[dict[str, Any]]:
    return [
        {"id": "youtube", "label": "YouTube Shorts", "mode": "direct_upload", "token_env": PLATFORM_ENV["youtube"], "configured": bool(os.getenv(PLATFORM_ENV["youtube"]))},
        {"id": "instagram", "label": "Instagram Reels", "mode": "public_url_container", "token_env": PLATFORM_ENV["instagram"], "configured": bool(os.getenv(PLATFORM_ENV["instagram"]))},
        {"id": "tiktok", "label": "TikTok", "mode": "direct_or_draft", "token_env": PLATFORM_ENV["tiktok"], "configured": bool(os.getenv(PLATFORM_ENV["tiktok"]))},
    ]


def account_view(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        config = json.loads(item.pop("config_json", "{}"))
    except json.JSONDecodeError:
        config = {}
    token_env = config.get("token_env") or PLATFORM_ENV.get(item["platform"], "")
    item["config"] = {"token_env": token_env}
    item["status"] = "connected" if token_env and os.getenv(token_env) else "not_connected"
    return item


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
    account_id: str | None = None,
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
        with db.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM publish_jobs WHERE project_id = ? AND platform = ? AND status IN ('queued', 'ready') ORDER BY created_at DESC LIMIT 1",
                (project["id"], platform),
            ).fetchone()
        if existing:
            existing_item = dict(existing)
            existing_payload = json.loads(existing_item.get("payload_json", "{}"))
            if existing_payload.get("media_path") != str(export):
                existing_payload.update({"title": project.get("title") or project.get("name"), "caption": project.get("caption", ""), "media_path": str(export)})
                db.update("publish_jobs", "id", existing_item["id"], {"payload_json": json_dumps(existing_payload), "updated_at": now_iso()})
                existing_item = db.get("publish_jobs", "id", existing_item["id"]) or existing_item
            jobs.append(existing_item)
            continue
        job_id = new_id("publish")
        now = now_iso()
        selected_account_id = account_id
        if selected_account_id:
            account = db.get("publishing_accounts", "id", selected_account_id)
            if not account or account["platform"] != platform:
                raise ValueError(f"Account does not match {platform}")
        else:
            with db.connection() as connection:
                account = connection.execute("SELECT * FROM publishing_accounts WHERE platform = ? ORDER BY created_at LIMIT 1", (platform,)).fetchone()
            selected_account_id = account["id"] if account else None
        payload = {
            "platform": platform,
            "title": project.get("title") or project.get("name"),
            "caption": project.get("caption", ""),
            "media_path": str(export),
            "requires_public_url": PLATFORM_SPECS[platform]["requires_public_url"],
            "state": "awaiting_account_connection",
            "privacy_status": "private",
        }
        db.insert(
            "publish_jobs",
            {
                "id": job_id,
                "project_id": project["id"],
                "account_id": selected_account_id,
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


def _http_error(error: urllib.error.HTTPError) -> RuntimeError:
    detail = error.read().decode("utf-8", "replace")[:500]
    return RuntimeError(f"Publisher returned HTTP {error.code}: {detail}")


def publish_youtube(payload: dict[str, Any], access_token: str) -> str:
    path = Path(payload["media_path"])
    if not path.is_file():
        raise ValueError("YouTube export is missing")
    body = {
        "snippet": {
            "title": str(payload.get("title") or "DeReel short")[:100],
            "description": str(payload.get("caption") or "")[:5000],
            "categoryId": "22",
        },
        "status": {"privacyStatus": payload.get("privacy_status") or "private", "selfDeclaredMadeForKids": False},
    }
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    start_url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
    start_request = urllib.request.Request(
        start_url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(path.stat().st_size),
            "X-Upload-Content-Type": media_type,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(start_request, timeout=60) as response:
            upload_url = response.headers.get("Location")
    except urllib.error.HTTPError as error:
        raise _http_error(error) from error
    if not upload_url:
        raise RuntimeError("YouTube did not return a resumable upload URL")
    upload_request = urllib.request.Request(
        upload_url,
        data=path.read_bytes(),
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": media_type},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(upload_request, timeout=900) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _http_error(error) from error
    video_id = result.get("id")
    if not video_id:
        raise RuntimeError("YouTube upload returned no video id")
    return str(video_id)


def publish_job(db: Database, job: dict[str, Any]) -> str:
    payload = json.loads(job.get("payload_json", "{}"))
    account = db.get("publishing_accounts", "id", job.get("account_id", "")) if job.get("account_id") else None
    if not account:
        with db.connection() as connection:
            account_row = connection.execute("SELECT * FROM publishing_accounts WHERE platform = ? ORDER BY created_at LIMIT 1", (job["platform"],)).fetchone()
        account = dict(account_row) if account_row else None
    if not account:
        raise ValueError(f"No {job['platform']} account is connected to this publish job")
    config = json.loads(account.get("config_json", "{}"))
    token_env = config.get("token_env") or PLATFORM_ENV.get(job["platform"], "")
    token = os.getenv(token_env)
    if not token:
        raise ValueError(f"Set {token_env} before publishing to {job['platform']}")
    if job["platform"] == "youtube":
        return publish_youtube(payload, token)
    if job["platform"] == "instagram":
        raise ValueError("Instagram requires a publicly reachable video URL for its media container")
    if job["platform"] == "tiktok":
        raise ValueError("TikTok publishing requires an approved Direct Post or draft-upload app")
    raise ValueError("Unsupported publisher")
