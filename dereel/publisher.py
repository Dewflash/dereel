from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database, json_dumps, json_loads, new_id, now_iso
from .media import probe, sha256_file


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


def _is_https_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def account_readiness(row: dict[str, Any]) -> dict[str, Any]:
    config = json_loads(row.get("config_json"), {})
    platform = str(row.get("platform") or "")
    token_env = str(config.get("token_env") or PLATFORM_ENV.get(platform, ""))
    token_configured = bool(str(os.getenv(token_env) or "").strip()) if token_env else False
    issues: list[str] = []
    if not token_configured:
        issues.append(f"Set {token_env or 'a platform access token'}")
    if platform == "instagram":
        if not str(config.get("instagram_user_id") or os.getenv("DEREEL_INSTAGRAM_USER_ID") or "").strip():
            issues.append("Instagram professional account ID is missing")
        public_base = str(config.get("public_media_base_url") or os.getenv("DEREEL_INSTAGRAM_PUBLIC_MEDIA_BASE_URL") or "").strip()
        if not _is_https_url(public_base):
            issues.append("HTTPS public media base URL is missing")
    return {"ready": not issues, "issues": issues, "token_env": token_env, "token_configured": token_configured}


def account_view(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    config = json_loads(item.pop("config_json", "{}"), {})
    readiness = account_readiness(row)
    item["config"] = {
        "token_env": readiness["token_env"],
        "instagram_user_id": config.get("instagram_user_id", ""),
        "public_media_base_url": config.get("public_media_base_url", ""),
    }
    item["readiness"] = readiness
    item["status"] = "connected" if readiness["token_configured"] else "not_connected"
    return item


def update_publishing_account(db: Database, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    account = db.get("publishing_accounts", "id", account_id)
    if not account:
        raise KeyError("Publishing account not found")
    label = str(payload.get("label", account["label"]) or account["platform"].title()).strip()[:120]
    config = json_loads(account.get("config_json"), {})
    if account["platform"] == "instagram":
        if "instagram_user_id" in payload:
            config["instagram_user_id"] = str(payload.get("instagram_user_id") or "").strip()
        if "public_media_base_url" in payload:
            config["public_media_base_url"] = str(payload.get("public_media_base_url") or "").strip().rstrip("/")
    db.update("publishing_accounts", "id", account_id, {"label": label, "config_json": json_dumps(config), "updated_at": now_iso()})
    return account_view(db.get("publishing_accounts", "id", account_id) or {})


def delete_publishing_account(db: Database, account_id: str) -> None:
    with db.connection() as connection:
        account = connection.execute("SELECT id FROM publishing_accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            raise KeyError("Publishing account not found")
        pending = connection.execute(
            "SELECT id FROM publish_jobs WHERE account_id = ? AND (status IN ('queued', 'ready') OR (claim_token IS NOT NULL AND claim_token != '')) LIMIT 1",
            (account_id,),
        ).fetchone()
        if pending:
            raise ValueError("Account cannot be deleted while it has a pending publish job")
        connection.execute("DELETE FROM publishing_accounts WHERE id = ?", (account_id,))


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


def publish_provenance(project: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    manifest = project.get("manifest") if isinstance(project.get("manifest"), dict) else json_loads(project.get("manifest_json"), {})
    inputs = list(manifest.get("inputs") or [])
    tracks = list((manifest.get("audio") or {}).get("tracks") or [])
    asset_ids = list(dict.fromkeys([str(item["asset_id"]) for item in [*inputs, *tracks] if item.get("asset_id")]))
    return {
        "asset_ids": asset_ids,
        "source_count": len(inputs),
        "audio_count": len(tracks),
        "manifest_path": str(output_dir / "render-manifest.json"),
        "credits_path": str(output_dir / "credits.json"),
    }


def _write_publish_manifest(output_dir: Path, job_id: str, payload: dict[str, Any], scheduled_for: str | None) -> None:
    manifest_path = output_dir / "publish-manifest.json"
    existing = json_loads(manifest_path.read_text(), {}) if manifest_path.exists() else {}
    if not isinstance(existing, dict):
        existing = {}
    jobs = [item for item in existing.get("jobs", []) if isinstance(item, dict) and item.get("id") != job_id]
    jobs.append({"id": job_id, **payload, "scheduled_for": scheduled_for or ""})
    existing["jobs"] = jobs
    manifest_path.write_text(json.dumps(existing, indent=2))


def create_publish_jobs(
    db: Database,
    project: dict[str, Any],
    platforms: list[str],
    scheduled_for: str | None = None,
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    if project.get("status") != "ready":
        raise ValueError("Project must be rendered and ready before creating publish jobs")
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
        export_sha256 = sha256_file(export)
        with db.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM publish_jobs WHERE project_id = ? AND platform = ? AND status IN ('queued', 'ready') ORDER BY created_at DESC LIMIT 1",
                (project["id"], platform),
            ).fetchone()
        if existing:
            existing_item = dict(existing)
            existing_payload = json_loads(existing_item.get("payload_json"), {})
            if not isinstance(existing_payload, dict):
                existing_payload = {}
            desired_payload = {
                "title": project.get("title") or project.get("name"),
                "caption": project.get("caption", ""),
                "media_path": str(export),
                "media_sha256": export_sha256,
                "requires_public_url": PLATFORM_SPECS[platform]["requires_public_url"],
                "provenance": publish_provenance(project, output_dir),
            }
            if any(existing_payload.get(key) != value for key, value in desired_payload.items()):
                existing_payload.update(desired_payload)
                db.update("publish_jobs", "id", existing_item["id"], {"payload_json": json_dumps(existing_payload), "updated_at": now_iso()})
                existing_item = db.get("publish_jobs", "id", existing_item["id"]) or existing_item
            jobs.append(existing_item)
            _write_publish_manifest(output_dir, existing_item["id"], existing_payload, scheduled_for if scheduled_for is not None else existing_item.get("scheduled_for"))
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
            "project_id": project["id"],
            "title": project.get("title") or project.get("name"),
            "caption": project.get("caption", ""),
            "media_path": str(export),
            "media_sha256": export_sha256,
            "requires_public_url": PLATFORM_SPECS[platform]["requires_public_url"],
            "state": "awaiting_account_connection",
            "privacy_status": "private",
            "provenance": publish_provenance(project, output_dir),
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
        _write_publish_manifest(output_dir, job_id, payload, scheduled_for)
    return jobs


def backfill_publish_integrity(db: Database) -> int:
    updated = 0
    with db.connection() as connection:
        rows = connection.execute("SELECT id, project_id, payload_json FROM publish_jobs WHERE status IN ('ready', 'failed') AND cancelled_at IS NULL").fetchall()
        for row in rows:
            payload = json_loads(row["payload_json"], {})
            if not isinstance(payload, dict):
                continue
            media_path = Path(str(payload.get("media_path") or ""))
            changed = False
            if not payload.get("media_sha256") and media_path.is_file():
                try:
                    payload["media_sha256"] = sha256_file(media_path)
                    changed = True
                except OSError:
                    pass
            if not payload.get("provenance"):
                project_row = connection.execute("SELECT * FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
                if project_row:
                    project = dict(project_row)
                    output_dir = Path(str(project.get("output_dir") or media_path.parent))
                    payload["provenance"] = publish_provenance(project, output_dir)
                    changed = True
            if changed:
                connection.execute("UPDATE publish_jobs SET payload_json = ?, updated_at = ? WHERE id = ?", (json_dumps(payload), now_iso(), row["id"]))
                updated += 1
    return updated


def claim_publish_job(db: Database, job_id: str) -> bool:
    token = new_id("claim")
    with db.connection() as connection:
        result = connection.execute(
            "UPDATE publish_jobs SET claim_token = ?, claimed_at = ?, error = '' WHERE id = ? AND status IN ('ready', 'failed') AND cancelled_at IS NULL AND (claim_token IS NULL OR claim_token = '')",
            (token, now_iso(), job_id),
        )
    return result.rowcount == 1


def begin_publish_attempt(db: Database, job_id: str, trigger: str) -> dict[str, Any]:
    if trigger not in {"manual", "scheduler"}:
        raise ValueError("Unsupported publish trigger")
    attempt_id = new_id("attempt")
    now = now_iso()
    with db.connection() as connection:
        job = connection.execute("SELECT claim_token FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job or not job["claim_token"]:
            raise ValueError("Publish job is not owned by an active local attempt")
        next_number = connection.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM publish_attempts WHERE publish_job_id = ?",
            (job_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO publish_attempts (id, publish_job_id, attempt_number, trigger, status, remote_id, error, started_at, finished_at, updated_at) VALUES (?, ?, ?, ?, 'running', '', '', ?, NULL, ?)",
            (attempt_id, job_id, next_number, trigger, now, now),
        )
    return db.get("publish_attempts", "id", attempt_id) or {}


def complete_publish_attempt(
    db: Database,
    attempt_id: str,
    status: str,
    remote_id: str = "",
    error: str = "",
) -> dict[str, Any]:
    if status not in {"published", "failed"}:
        raise ValueError("Unsupported publish completion status")
    finished = now_iso()
    with db.connection() as connection:
        attempt = connection.execute("SELECT * FROM publish_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not attempt or attempt["status"] != "running":
            raise ValueError("Publish attempt is not running")
        job = connection.execute("SELECT * FROM publish_jobs WHERE id = ?", (attempt["publish_job_id"],)).fetchone()
        if not job or not job["claim_token"]:
            raise ValueError("Publish job is not owned by an active local attempt")
        attempt_result = connection.execute(
            "UPDATE publish_attempts SET status = ?, remote_id = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (status, remote_id or "", error or "", finished, finished, attempt_id),
        )
        job_result = connection.execute(
            "UPDATE publish_jobs SET status = ?, remote_id = ?, error = ?, claim_token = NULL, claimed_at = NULL, updated_at = ? WHERE id = ? AND cancelled_at IS NULL AND claim_token IS NOT NULL AND claim_token != ''",
            (status, remote_id or "", error or "", finished, attempt["publish_job_id"]),
        )
        if attempt_result.rowcount != 1 or job_result.rowcount != 1:
            raise ValueError("Publish attempt could not be finalized")
    return db.get("publish_jobs", "id", attempt["publish_job_id"]) or {}


def complete_publish_job(db: Database, job_id: str, status: str, remote_id: str = "", error: str = "") -> dict[str, Any]:
    if status not in {"published", "failed"}:
        raise ValueError("Unsupported publish completion status")
    with db.connection() as connection:
        result = connection.execute(
            "UPDATE publish_jobs SET status = ?, remote_id = ?, error = ?, claim_token = NULL, claimed_at = NULL, updated_at = ? WHERE id = ? AND claim_token IS NOT NULL AND claim_token != ''",
            (status, remote_id, error, now_iso(), job_id),
        )
    if result.rowcount != 1:
        raise ValueError("Publish job is not owned by an active local attempt")
    return db.get("publish_jobs", "id", job_id) or {}


def fail_publish_attempt(db: Database, job_id: str, attempt_id: str = "", error: str = "") -> dict[str, Any]:
    timestamp = now_iso()
    with db.connection() as connection:
        if attempt_id:
            connection.execute(
                "UPDATE publish_attempts SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                (str(error or "Publish attempt failed")[:1000], timestamp, timestamp, attempt_id),
            )
        connection.execute(
            "UPDATE publish_jobs SET status = 'failed', error = ?, claim_token = NULL, claimed_at = NULL, updated_at = ? WHERE id = ? AND cancelled_at IS NULL AND claim_token IS NOT NULL AND claim_token != ''",
            (str(error or "Publish attempt failed")[:1000], timestamp, job_id),
        )
    return db.get("publish_jobs", "id", job_id) or {}


def force_publish_success(db: Database, job_id: str, attempt_id: str, remote_id: str) -> dict[str, Any]:
    timestamp = now_iso()
    with db.connection() as connection:
        attempt_result = connection.execute(
            "UPDATE publish_attempts SET status = 'published', remote_id = ?, error = '', finished_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
            (remote_id or "", timestamp, timestamp, attempt_id),
        )
        if attempt_result.rowcount != 1:
            raise ValueError("Publish attempt could not be force-finalized")
        job_result = connection.execute(
            "UPDATE publish_jobs SET status = 'published', remote_id = ?, error = '', claim_token = NULL, claimed_at = NULL, updated_at = ? WHERE id = ? AND cancelled_at IS NULL AND claim_token IS NOT NULL AND claim_token != ''",
            (remote_id or "", timestamp, job_id),
        )
        if job_result.rowcount != 1:
            raise ValueError("Publish job could not be force-finalized")
    return db.get("publish_jobs", "id", job_id) or {}


def mark_publish_job(db: Database, job_id: str, remote_id: str = "manual") -> dict[str, Any]:
    timestamp = now_iso()
    remote = remote_id or "manual"
    with db.connection() as connection:
        result = connection.execute(
            "UPDATE publish_jobs SET status = 'published', remote_id = ?, error = '', claim_token = NULL, claimed_at = NULL, updated_at = ? WHERE id = ? AND status IN ('ready', 'failed') AND cancelled_at IS NULL AND (claim_token IS NULL OR claim_token = '')",
            (remote, timestamp, job_id),
        )
        if result.rowcount != 1:
            raise ValueError("Publish job is already published or currently being processed")
        next_number = connection.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM publish_attempts WHERE publish_job_id = ?",
            (job_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO publish_attempts (id, publish_job_id, attempt_number, trigger, status, remote_id, error, started_at, finished_at, updated_at) VALUES (?, ?, ?, 'manual', 'published', ?, '', ?, ?, ?)",
            (new_id("attempt"), job_id, next_number, remote, timestamp, timestamp, timestamp),
        )
    return db.get("publish_jobs", "id", job_id) or {}


def publish_job_view(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["processing"] = bool(item.pop("claim_token", ""))
    item["cancelled"] = bool(item.get("cancelled_at"))
    if item["cancelled"]:
        item["status"] = "cancelled"
    try:
        item["payload"] = json.loads(item.pop("payload_json", "{}"))
    except json.JSONDecodeError:
        item["payload"] = {}
    return item


def list_publish_attempts(db: Database, job_id: str) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM publish_attempts WHERE publish_job_id = ? ORDER BY attempt_number DESC",
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def reschedule_publish_job(db: Database, job_id: str, scheduled_for: str) -> dict[str, Any]:
    schedule = str(scheduled_for or "").strip()
    if schedule:
        try:
            parsed = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Invalid scheduled_for timestamp") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        schedule = parsed.astimezone(timezone.utc).isoformat()
    with db.connection() as connection:
        result = connection.execute(
            "UPDATE publish_jobs SET status = 'ready', scheduled_for = ?, error = '', updated_at = ? WHERE id = ? AND status IN ('ready', 'failed') AND cancelled_at IS NULL AND (claim_token IS NULL OR claim_token = '')",
            (schedule, now_iso(), job_id),
        )
    if result.rowcount != 1:
        raise ValueError("Publish job is not available to reschedule")
    return publish_job_view(db.get("publish_jobs", "id", job_id) or {})


def cancel_publish_job(db: Database, job_id: str) -> dict[str, Any]:
    timestamp = now_iso()
    with db.connection() as connection:
        result = connection.execute(
            "UPDATE publish_jobs SET status = 'failed', error = 'Cancelled by operator', cancelled_at = ?, updated_at = ? WHERE id = ? AND status IN ('ready', 'failed') AND cancelled_at IS NULL AND (claim_token IS NULL OR claim_token = '')",
            (timestamp, timestamp, job_id),
        )
    if result.rowcount != 1:
        raise ValueError("Publish job is already published, cancelled, or being processed")
    return publish_job_view(db.get("publish_jobs", "id", job_id) or {})


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


def publish_tiktok_draft(payload: dict[str, Any], access_token: str) -> str:
    path = Path(payload["media_path"])
    if not path.is_file():
        raise ValueError("TikTok export is missing")
    total_size = path.stat().st_size
    if total_size <= 0:
        raise ValueError("TikTok export is empty")
    chunk_size = total_size if total_size < 5 * 1024 * 1024 else min(10 * 1024 * 1024, total_size)
    total_chunks = (total_size + chunk_size - 1) // chunk_size
    init_body = {"source_info": {"source": "FILE_UPLOAD", "video_size": total_size, "chunk_size": chunk_size, "total_chunk_count": total_chunks}}
    init_request = urllib.request.Request(
        "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
        data=json.dumps(init_body).encode(),
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(init_request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _http_error(error) from error
    error = result.get("error", {})
    if error.get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok returned {error.get('code')}: {error.get('message', '')}")
    data = result.get("data") or {}
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if not upload_url or not publish_id:
        raise RuntimeError("TikTok did not return a draft upload URL")
    with path.open("rb") as source:
        offset = 0
        while offset < total_size:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            upload_request = urllib.request.Request(
                upload_url,
                data=chunk,
                headers={"Content-Type": "video/mp4", "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{total_size}"},
                method="PUT",
            )
            try:
                with urllib.request.urlopen(upload_request, timeout=900) as response:
                    if response.status not in (200, 201, 206):
                        raise RuntimeError(f"TikTok upload returned HTTP {response.status}")
            except urllib.error.HTTPError as error:
                raise _http_error(error) from error
            offset = end + 1
    return f"draft:{publish_id}"


def _graph_url(version: str, path: str) -> str:
    return f"https://graph.facebook.com/{version}/{path.lstrip('/')}"


def _read_json_response(response: Any) -> dict[str, Any]:
    try:
        result = json.loads(response.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Publisher returned invalid JSON") from error
    if not isinstance(result, dict):
        raise RuntimeError("Publisher returned an invalid response")
    if result.get("error"):
        detail = result["error"]
        if isinstance(detail, dict):
            raise RuntimeError(f"Instagram returned {detail.get('code')}: {detail.get('message', '')}")
        raise RuntimeError(f"Instagram returned an error: {detail}")
    return result


def publish_instagram_reel(payload: dict[str, Any], account: dict[str, Any], access_token: str) -> str:
    config = json.loads(account.get("config_json", "{}"))
    user_id = str(config.get("instagram_user_id") or os.getenv("DEREEL_INSTAGRAM_USER_ID") or "").strip()
    public_base = str(config.get("public_media_base_url") or os.getenv("DEREEL_INSTAGRAM_PUBLIC_MEDIA_BASE_URL") or "").strip().rstrip("/")
    project_id = str(payload.get("project_id") or "").strip()
    platform = str(payload.get("platform") or "instagram").strip()
    if not user_id:
        raise ValueError("Instagram user ID is required for Reels publishing")
    if not _is_https_url(public_base):
        raise ValueError("Instagram requires an HTTPS public media base URL")
    if not project_id:
        raise ValueError("Instagram publish job is missing its project ID")
    public_media_url = str(payload.get("public_media_url") or f"{public_base}/api/projects/{urllib.parse.quote(project_id)}/output/{platform}.mp4")
    if not _is_https_url(public_media_url):
        raise ValueError("Instagram media URL must use HTTPS")
    version = os.getenv("DEREEL_INSTAGRAM_GRAPH_VERSION", "v22.0")
    user_path = urllib.parse.quote(user_id, safe="")
    common = {"access_token": access_token}
    container_fields = {
        **common,
        "media_type": "REELS",
        "video_url": public_media_url,
        "caption": str(payload.get("caption") or "")[:2200],
        "share_to_feed": "true",
    }
    container_request = urllib.request.Request(
        _graph_url(version, f"{user_path}/media"),
        data=urllib.parse.urlencode(container_fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(container_request, timeout=60) as response:
            container = _read_json_response(response)
    except urllib.error.HTTPError as error:
        raise _http_error(error) from error
    container_id = container.get("id")
    if not container_id:
        raise RuntimeError("Instagram did not return a media container ID")

    status_url = _graph_url(version, str(container_id)) + "?" + urllib.parse.urlencode({"fields": "status_code,status", **common})
    deadline = time.monotonic() + 150
    while True:
        status_request = urllib.request.Request(status_url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(status_request, timeout=60) as response:
                status = _read_json_response(response)
        except urllib.error.HTTPError as error:
            raise _http_error(error) from error
        status_code = str(status.get("status_code") or status.get("status") or "").upper()
        if status_code in {"FINISHED", "READY"}:
            break
        if status_code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram media container is {status_code.lower()}")
        if time.monotonic() >= deadline:
            raise RuntimeError("Instagram media container did not finish processing in time")
        time.sleep(5)

    publish_request = urllib.request.Request(
        _graph_url(version, f"{user_path}/media_publish"),
        data=urllib.parse.urlencode({"creation_id": container_id, **common}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(publish_request, timeout=60) as response:
            published = _read_json_response(response)
    except urllib.error.HTTPError as error:
        raise _http_error(error) from error
    media_id = published.get("id")
    if not media_id:
        raise RuntimeError("Instagram did not return a published media ID")
    return str(media_id)


def publish_job(db: Database, job: dict[str, Any]) -> str:
    if job.get("status") == "published":
        raise ValueError("Publish job is already marked published")
    if job.get("cancelled_at"):
        raise ValueError("Publish job is cancelled")
    if job.get("status") not in {"ready", "failed"}:
        raise ValueError("Publish job is not ready to run")
    payload = json.loads(job.get("payload_json", "{}"))
    export_path = Path(payload.get("media_path") or "")
    problems = validate_export(export_path, str(job.get("platform") or ""))
    if problems:
        raise ValueError(f"{PLATFORM_SPECS.get(job.get('platform', ''), {}).get('label', 'Publish export')}: " + "; ".join(problems))
    expected_sha256 = str(payload.get("media_sha256") or "").strip()
    if expected_sha256 and sha256_file(export_path) != expected_sha256:
        raise ValueError("Publish export changed after the outbox package was prepared")
    account = db.get("publishing_accounts", "id", job.get("account_id", "")) if job.get("account_id") else None
    if not account:
        with db.connection() as connection:
            account_row = connection.execute("SELECT * FROM publishing_accounts WHERE platform = ? ORDER BY created_at LIMIT 1", (job["platform"],)).fetchone()
        account = dict(account_row) if account_row else None
    if not account:
        raise ValueError(f"No {job['platform']} account is connected to this publish job")
    readiness = account_readiness(account)
    if not readiness["ready"]:
        raise ValueError(f"{PLATFORM_SPECS[job['platform']]['label']} account is not ready: " + "; ".join(readiness["issues"]))
    token = str(os.getenv(readiness["token_env"]) or "").strip()
    if job["platform"] == "youtube":
        return publish_youtube(payload, token)
    if job["platform"] == "instagram":
        return publish_instagram_reel(payload, account, token)
    if job["platform"] == "tiktok":
        return publish_tiktok_draft(payload, token)
    raise ValueError("Unsupported publisher")
