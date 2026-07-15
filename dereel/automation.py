from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .db import Database, json_dumps, json_loads, new_id, now_iso
from .media import VIDEO_EXTENSIONS, atomic_copy_file, safe_extension, sha256_file, validate_video
from .pipeline import PRODUCTION_RIGHTS, cleanup_pending_asset, create_asset, create_project, plan_project, production_health_error, render_project, review_asset
from .publisher import create_publish_jobs
from .scout import import_provider_asset, search as search_provider


AUTOMATION_RIGHTS = {"pending_review", "owned_distribution", "licensed_distribution"}
AUTOMATION_PLATFORMS = {"youtube", "instagram", "tiktok"}
AUTOMATION_SOURCE_MODES = {"watch", "provider", "watch_provider"}
AUTOMATION_PROVIDERS = {"pexels", "pixabay"}
MAX_WATCH_BYTES = 800 * 1024 * 1024
AUTOMATION_RUN_STATUSES = {"running", "idle", "no_sources", "awaiting_review", "planned", "rendered", "failed"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(minimum, min(parsed, maximum)), 3)


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return sorted({str(item).strip().lower() for item in values if str(item).strip()})[:24]


def _platforms(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(item).strip().lower() for item in values if str(item).strip() in AUTOMATION_PLATFORMS))


def _account_ids(db: Database, value: Any, platforms: list[str]) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Automation account_ids must be an object")
    result: dict[str, str] = {}
    for platform, account_id in value.items():
        normalized_platform = str(platform or "").strip().lower()
        normalized_id = str(account_id or "").strip()
        if normalized_platform and normalized_id:
            result[normalized_platform] = normalized_id
    unknown_platforms = set(result) - set(platforms)
    if unknown_platforms:
        raise ValueError("Automation account selection includes an unselected platform")
    if not result:
        return result
    with db.connection() as connection:
        for platform, account_id in result.items():
            account = connection.execute("SELECT platform FROM publishing_accounts WHERE id = ?", (account_id,)).fetchone()
            if not account or account["platform"] != platform:
                raise ValueError(f"Automation account does not match {platform}")
    return result


def automation_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["tags"] = json_loads(result.pop("tags_json", "[]"), [])
    result["platforms"] = json_loads(result.pop("platforms_json", "[]"), [])
    account_ids = json_loads(result.pop("account_ids_json", "{}"), {})
    result["account_ids"] = account_ids if isinstance(account_ids, dict) else {}
    result["auto_render"] = bool(result.get("auto_render"))
    result["enabled"] = bool(result.get("enabled"))
    return result


def automation_run_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["scan"] = json_loads(result.pop("scan_json", "{}"), {})
    result["result"] = json_loads(result.pop("result_json", "{}"), {})
    return result


def create_automation_run(db: Database, automation_id: str, job_id: str) -> dict[str, Any]:
    run_id = new_id("run")
    now = now_iso()
    db.insert(
        "automation_runs",
        {
            "id": run_id,
            "automation_id": automation_id,
            "job_id": job_id,
            "project_id": None,
            "status": "running",
            "scan_json": "{}",
            "result_json": "{}",
            "error": "",
            "started_at": now,
            "finished_at": None,
            "updated_at": now,
        },
    )
    return automation_run_view(db.get("automation_runs", "id", run_id) or {})


def finish_automation_run(db: Database, run_id: str, result: dict[str, Any] | None = None, error: str = "") -> dict[str, Any]:
    result = result or {}
    status = "failed" if error else str(result.get("status") or "idle")
    if status not in AUTOMATION_RUN_STATUSES:
        status = "idle"
    scan = result.get("scan") if isinstance(result.get("scan"), dict) else {}
    project_id = str(result.get("project_id") or "") or None
    now = now_iso()
    db.update(
        "automation_runs",
        "id",
        run_id,
        {
            "status": status,
            "project_id": project_id,
            "scan_json": json_dumps(scan),
            "result_json": json_dumps(result),
            "error": error,
            "finished_at": now,
            "updated_at": now,
        },
    )
    return automation_run_view(db.get("automation_runs", "id", run_id) or {})


def list_automation_runs(db: Database, automation_id: str, limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM automation_runs WHERE automation_id = ? ORDER BY started_at DESC LIMIT ?",
            (automation_id, limit),
        ).fetchall()
    return [automation_run_view(dict(row)) for row in rows]


def _validate_audio_asset(db: Database, asset_id: str, role: str) -> None:
    if not asset_id:
        return
    asset = db.get("assets", "id", asset_id)
    if not asset or not asset.get("local_path") or not str(asset.get("mime_type") or "").startswith("audio/"):
        raise ValueError(f"Automation {role} is not a valid audio asset")
    if asset.get("rights_status") not in PRODUCTION_RIGHTS or asset.get("status") not in {"ready", "approved"}:
        raise ValueError(f"Automation {role} is not cleared for production")
    health_error = production_health_error(asset)
    if health_error:
        raise ValueError(f"Automation {role} is not ready for production: {health_error}")


def _normalized_payload(db: Database, payload: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    current = current or {}
    watch_dir_value = payload.get("watch_dir", current.get("watch_dir", ""))
    watch_dir = str(watch_dir_value or "").strip()
    default_rights = str(payload.get("default_rights", current.get("default_rights", "pending_review")) or "pending_review")
    if default_rights not in AUTOMATION_RIGHTS:
        raise ValueError("Unsupported automation rights mode")
    source_mode = str(payload.get("source_mode", current.get("source_mode", "watch")) or "watch").strip().lower()
    if source_mode not in AUTOMATION_SOURCE_MODES:
        raise ValueError("Unsupported automation source mode")
    provider = str(payload.get("provider", current.get("provider", "")) or "").strip().lower()
    search_query = str(payload.get("search_query", current.get("search_query", "")) or "").strip()[:100]
    if source_mode in {"provider", "watch_provider"}:
        if provider not in AUTOMATION_PROVIDERS:
            raise ValueError("A supported provider is required for this source mode")
        if not search_query:
            raise ValueError("A provider search query is required")
    if source_mode in {"watch", "watch_provider"} and not str(watch_dir_value or "").strip():
        raise ValueError("A watch folder is required for this source mode")
    provider_limit = _as_int(payload.get("provider_limit", current.get("provider_limit", 12)), 12, 1, 24)
    platforms = _platforms(payload.get("platforms", current.get("platforms", [])))
    requested_account_ids = payload.get("account_ids", current.get("account_ids", {}))
    if "platforms" in payload and "account_ids" not in payload and isinstance(requested_account_ids, dict):
        requested_account_ids = {platform: account_id for platform, account_id in requested_account_ids.items() if str(platform).strip().lower() in platforms}
    account_ids = _account_ids(db, requested_account_ids, platforms)
    music_asset_id = str(payload.get("music_asset_id", current.get("music_asset_id", "")) or "").strip()
    sfx_asset_id = str(payload.get("sfx_asset_id", current.get("sfx_asset_id", "")) or "").strip()
    voiceover_asset_id = str(payload.get("voiceover_asset_id", current.get("voiceover_asset_id", "")) or "").strip()
    _validate_audio_asset(db, music_asset_id, "music")
    _validate_audio_asset(db, sfx_asset_id, "sound effect")
    _validate_audio_asset(db, voiceover_asset_id, "voiceover")
    return {
        "name": str(payload.get("name", current.get("name", "Untitled automation")) or "Untitled automation").strip()[:120],
        "watch_dir": str(Path(watch_dir).expanduser().resolve()) if watch_dir else "",
        "source_mode": source_mode,
        "provider": provider,
        "search_query": search_query,
        "provider_limit": provider_limit,
        "theme": str(payload.get("theme", current.get("theme", "funny moments")) or "funny moments").strip()[:180],
        "duration_target": max(8, min(float(payload.get("duration_target", current.get("duration_target", 35)) or 35), 180)),
        "style": str(payload.get("style", current.get("style", "kinetic")) or "kinetic").strip()[:40],
        "title_template": str(payload.get("title_template", current.get("title_template", "")) or "").strip()[:100],
        "caption_template": str(payload.get("caption_template", current.get("caption_template", "")) or "").strip()[:500],
        "tags_json": json_dumps(_tags(payload.get("tags", current.get("tags", [])))),
        "default_rights": default_rights,
        "music_asset_id": music_asset_id,
        "music_volume": _as_float(payload.get("music_volume", current.get("music_volume", 0.16)), 0.16, 0.0, 1.0),
        "sfx_asset_id": sfx_asset_id,
        "sfx_volume": _as_float(payload.get("sfx_volume", current.get("sfx_volume", 0.55)), 0.55, 0.0, 1.0),
        "sfx_start_time": _as_float(payload.get("sfx_start_time", current.get("sfx_start_time", 0)), 0.0, 0.0, 3600.0),
        "voiceover_asset_id": voiceover_asset_id,
        "voiceover_volume": _as_float(payload.get("voiceover_volume", current.get("voiceover_volume", 1.0)), 1.0, 0.0, 1.0),
        "voiceover_start_time": _as_float(payload.get("voiceover_start_time", current.get("voiceover_start_time", 0)), 0.0, 0.0, 3600.0),
        "platforms_json": json_dumps(platforms),
        "account_ids_json": json_dumps(account_ids),
        "interval_minutes": _as_int(payload.get("interval_minutes", current.get("interval_minutes", 0)), 0, 0, 10080),
        "publish_delay_minutes": _as_int(payload.get("publish_delay_minutes", current.get("publish_delay_minutes", 0)), 0, 0, 43200),
        "auto_render": int(_as_bool(payload.get("auto_render", current.get("auto_render", True)), True)),
        "enabled": int(_as_bool(payload.get("enabled", current.get("enabled", False)), False)),
    }


def create_automation(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
    values = _normalized_payload(db, payload)
    automation_id = new_id("automation")
    now = now_iso()
    db.insert(
        "automations",
        {
            "id": automation_id,
            **values,
            "status": "idle",
            "last_run_at": "",
            "last_error": "",
            "last_project_id": "",
            "created_at": now,
            "updated_at": now,
        },
    )
    return automation_view(db.get("automations", "id", automation_id) or {})


def update_automation(db: Database, automation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = db.get("automations", "id", automation_id)
    if not row:
        raise KeyError("Automation not found")
    if row.get("status") in {"queued", "running"} and set(payload) - {"enabled"}:
        raise ValueError("Automation configuration cannot change while it is processing")
    current = automation_view(row)
    values = _normalized_payload(db, payload, current)
    rebuild_keys = {
        "name", "watch_dir", "source_mode", "provider", "search_query", "provider_limit", "theme", "duration_target", "style",
        "title_template", "caption_template", "tags", "default_rights", "music_asset_id", "music_volume", "sfx_asset_id",
        "sfx_volume", "sfx_start_time", "voiceover_asset_id", "voiceover_volume", "voiceover_start_time",
    }
    if rebuild_keys.intersection(payload):
        values["last_project_id"] = ""
    values["updated_at"] = now_iso()
    removed_platforms = sorted(set(current.get("platforms") or []) - set(json_loads(values.get("platforms_json"), [])))
    project_id = str(current.get("last_project_id") or "")
    with db.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assignments = ", ".join(f"{column} = ?" for column in values)
        connection.execute(
            f"UPDATE automations SET {assignments} WHERE id = ?",
            [*values.values(), automation_id],
        )
        if removed_platforms and project_id:
            placeholders = ", ".join("?" for _ in removed_platforms)
            timestamp = now_iso()
            connection.execute(
                f"UPDATE publish_jobs SET status = 'failed', error = 'Destination removed from automation', cancelled_at = ?, updated_at = ? WHERE project_id = ? AND platform IN ({placeholders}) AND status IN ('queued', 'ready', 'failed') AND cancelled_at IS NULL AND (claim_token IS NULL OR claim_token = '')",
                [timestamp, timestamp, project_id, *removed_platforms],
            )
    return automation_view(db.get("automations", "id", automation_id) or {})


def list_automations(db: Database) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute("SELECT * FROM automations ORDER BY updated_at DESC").fetchall()
    return [automation_view(dict(row)) for row in rows]


def delete_automation(db: Database, automation_id: str) -> None:
    row = db.get("automations", "id", automation_id)
    if not row:
        raise KeyError("Automation not found")
    if row.get("status") in {"queued", "running"}:
        raise ValueError("Stop the running automation before deleting it")
    with db.connection() as connection:
        connection.execute("UPDATE assets SET automation_id = NULL WHERE automation_id = ?", (automation_id,))
        connection.execute("DELETE FROM automations WHERE id = ?", (automation_id,))


def claim_automation(db: Database, automation_id: str) -> bool:
    with db.connection() as connection:
        result = connection.execute(
            "UPDATE automations SET status = 'queued', last_error = '', updated_at = ? WHERE id = ? AND status NOT IN ('queued', 'running')",
            (now_iso(), automation_id),
        )
    return result.rowcount == 1


def set_automation_running(db: Database, automation_id: str) -> None:
    db.update("automations", "id", automation_id, {"status": "running", "updated_at": now_iso()})


def finish_automation(db: Database, automation_id: str, result: dict[str, Any] | None = None, error: str = "") -> None:
    result = result or {}
    values = {
        "status": "failed" if error else "idle",
        "last_run_at": now_iso(),
        "last_error": error,
        "updated_at": now_iso(),
    }
    if result.get("project_id"):
        values["last_project_id"] = str(result["project_id"])
    db.update("automations", "id", automation_id, values)


def due_automations(db: Database, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []
    for automation in list_automations(db):
        if not automation["enabled"] or automation["interval_minutes"] <= 0 or automation["status"] in {"queued", "running"}:
            continue
        last_run = str(automation.get("last_run_at") or "")
        if last_run:
            try:
                last_at = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=timezone.utc)
            except ValueError:
                last_at = datetime.min.replace(tzinfo=timezone.utc)
            if last_at + timedelta(minutes=automation["interval_minutes"]) > now:
                continue
        due.append(automation)
    return due


def scan_watch_folder(db: Database, automation: dict[str, Any], originals_dir: Path) -> dict[str, Any]:
    watch_dir = Path(automation["watch_dir"]).expanduser().resolve()
    if not watch_dir.is_dir():
        raise ValueError(f"Automation watch folder does not exist: {watch_dir}")
    originals_dir.mkdir(parents=True, exist_ok=True)
    imported: list[dict[str, Any]] = []
    duplicates: list[str] = []
    errors: list[dict[str, str]] = []
    files = sorted((item for item in watch_dir.iterdir() if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS), key=lambda item: item.name.lower())
    for source in files:
        destination: Path | None = None
        created_asset_id = ""
        try:
            if source.stat().st_size > MAX_WATCH_BYTES:
                raise ValueError(f"File exceeds the {MAX_WATCH_BYTES // (1024 * 1024)} MB limit")
            metadata = validate_video(source)
            fingerprint = sha256_file(source)
            with db.connection() as connection:
                duplicate = connection.execute("SELECT id FROM assets WHERE fingerprint = ? LIMIT 1", (fingerprint,)).fetchone()
            if duplicate:
                duplicates.append(source.name)
                continue
            asset_id = new_id("asset")
            content_type = mimetypes.guess_type(source.name)[0] or "video/mp4"
            destination = originals_dir / f"{asset_id}{safe_extension(source.name, content_type)}"
            atomic_copy_file(source, destination, max_bytes=MAX_WATCH_BYTES)
            copied_fingerprint = sha256_file(destination)
            if copied_fingerprint != fingerprint:
                raise ValueError("Source changed during import; retry the watch-folder scan")
            metadata["fingerprint"] = copied_fingerprint
            metadata["watch_source"] = str(source)
            requested_rights = automation["default_rights"]
            asset = create_asset(
                db,
                kind="media",
                title=source.stem,
                source_url=source.as_uri(),
                platform="local-watch",
                local_path=str(destination),
                mime_type=content_type,
                tags=automation["tags"],
                rights_status="pending_review",
                metadata=metadata,
                automation_id=automation["id"],
            )
            created_asset_id = asset["id"]
            if requested_rights != "pending_review":
                action = "approve_owned" if requested_rights == "owned_distribution" else "approve_licensed"
                asset = review_asset(db, asset["id"], action, f"Automation attestation: {requested_rights.replace('_', ' ')}")
            imported.append(asset)
        except Exception as error:
            if created_asset_id:
                cleanup_pending_asset(db, created_asset_id, str(destination or ""))
            elif destination:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            if "matches existing asset" in str(error):
                duplicates.append(source.name)
            else:
                errors.append({"file": source.name, "error": str(error)[:300]})
    return {"found": len(files), "imported": imported, "duplicates": duplicates, "errors": errors}


def _cleanup_provider_asset(db: Database, asset: dict[str, Any]) -> None:
    cleanup_pending_asset(db, str(asset.get("id") or ""), str(asset.get("local_path") or ""))


def scan_provider_sources(db: Database, automation: dict[str, Any], originals_dir: Path) -> dict[str, Any]:
    results = search_provider(automation["provider"], automation["search_query"], per_page=automation["provider_limit"])
    imported: list[dict[str, Any]] = []
    duplicates: list[str] = []
    errors: list[dict[str, str]] = []
    for result in results:
        asset: dict[str, Any] | None = None
        try:
            asset = import_provider_asset(
                db,
                originals_dir,
                {
                    "provider": result["provider"],
                    "provider_id": result.get("provider_id", ""),
                    "download_url": result["download_url"],
                    "source_url": result.get("source_url", ""),
                    "title": result.get("title", "Provider import"),
                    "tags": automation["tags"],
                },
                automation_id=automation["id"],
            )
            requested_rights = automation["default_rights"]
            if requested_rights != "pending_review":
                action = "approve_owned" if requested_rights == "owned_distribution" else "approve_licensed"
                asset = review_asset(db, asset["id"], action, f"Automation attestation: {requested_rights.replace('_', ' ')}")
            imported.append(asset)
        except ValueError as error:
            if asset:
                _cleanup_provider_asset(db, asset)
            if "matches existing asset" in str(error):
                duplicates.append(str(result.get("title") or result.get("provider_id") or "provider result"))
            else:
                errors.append({"file": str(result.get("title") or "provider result"), "error": str(error)[:300]})
        except Exception as error:
            if asset:
                _cleanup_provider_asset(db, asset)
            errors.append({"file": str(result.get("title") or "provider result"), "error": str(error)[:300]})
    return {"found": len(results), "imported": imported, "duplicates": duplicates, "errors": errors}


def _merge_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "found": sum(int(report.get("found") or 0) for report in reports),
        "imported": [asset for report in reports for asset in report.get("imported", [])],
        "duplicates": [item for report in reports for item in report.get("duplicates", [])],
        "errors": [item for report in reports for item in report.get("errors", [])],
    }


def _template(value: str, automation: dict[str, Any], run_date: str) -> str:
    return (value or "").replace("{name}", automation["name"]).replace("{theme}", automation["theme"]).replace("{date}", run_date)


def _automation_asset_ids(db: Database, automation_id: str) -> list[str]:
    with db.connection() as connection:
        rows = connection.execute("SELECT id FROM assets WHERE automation_id = ? AND kind = 'media' ORDER BY created_at DESC", (automation_id,)).fetchall()
    return [str(row[0]) for row in rows]


def _publish_job_platforms(db: Database, project_id: str, statuses: set[str] | None = None) -> set[str]:
    statuses = statuses or {"queued", "ready", "drafted", "published"}
    if not statuses:
        return set()
    placeholders = ", ".join("?" for _ in statuses)
    with db.connection() as connection:
        rows = connection.execute(
            f"SELECT DISTINCT platform FROM publish_jobs WHERE project_id = ? AND cancelled_at IS NULL AND status IN ({placeholders})",
            [project_id, *sorted(statuses)],
        ).fetchall()
    return {str(row["platform"]) for row in rows}


def _prepare_automation_publish_jobs(db: Database, automation: dict[str, Any], project_id: str, fallback_project: dict[str, Any] | None = None, platforms: list[str] | None = None) -> list[dict[str, Any]]:
    requested_platforms = platforms if platforms is not None else automation["platforms"]
    if not requested_platforms:
        return []
    scheduled_for = ""
    if automation["publish_delay_minutes"] > 0:
        scheduled_for = (datetime.now(timezone.utc) + timedelta(minutes=automation["publish_delay_minutes"])).replace(microsecond=0).isoformat()
    project = db.get("projects", "id", project_id) or fallback_project
    if not project:
        raise KeyError("Automation project not found")
    account_ids = {platform: account_id for platform, account_id in automation["account_ids"].items() if platform in requested_platforms}
    return create_publish_jobs(db, project, requested_platforms, scheduled_for, account_ids=account_ids)


def run_automation(
    db: Database,
    automation_id: str,
    originals_dir: Path,
    output_dir: Path,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    automation_row = db.get("automations", "id", automation_id)
    if not automation_row:
        raise KeyError("Automation not found")
    automation = automation_view(automation_row)
    reports: list[dict[str, Any]] = []
    if automation["source_mode"] in {"watch", "watch_provider"}:
        reports.append(scan_watch_folder(db, automation, originals_dir))
    if automation["source_mode"] in {"provider", "watch_provider"}:
        reports.append(scan_provider_sources(db, automation, originals_dir))
    report = _merge_reports(reports)
    if progress:
        progress(0.24, f"Imported {len(report['imported'])} new source{'' if len(report['imported']) == 1 else 's'}")
    asset_ids = _automation_asset_ids(db, automation_id)
    imported_ids = [asset["id"] for asset in report["imported"]]
    last_project = db.get("projects", "id", automation.get("last_project_id", "")) if automation.get("last_project_id") else None
    output_exists = bool(last_project and last_project.get("output_dir") and (Path(last_project["output_dir"]) / "final.mp4").is_file())
    if not imported_ids and last_project and last_project.get("status") == "ready" and output_exists:
        covered_platforms = _publish_job_platforms(db, last_project["id"])
        if not automation["platforms"]:
            return {"status": "idle", "project_id": last_project["id"], "scan": report, "message": "No new sources"}
        refreshable_platforms = _publish_job_platforms(db, last_project["id"], {"queued", "ready", "failed"})
        missing_platforms = [platform for platform in automation["platforms"] if platform not in covered_platforms]
        refresh_platforms = [platform for platform in automation["platforms"] if platform in refreshable_platforms]
        requested_platforms = list(dict.fromkeys([*missing_platforms, *refresh_platforms]))
        if not requested_platforms:
            return {"status": "idle", "project_id": last_project["id"], "scan": report, "message": "No new sources"}
        publish_jobs = _prepare_automation_publish_jobs(db, automation, last_project["id"], platforms=requested_platforms)
        return {"status": "rendered", "project_id": last_project["id"], "output_dir": last_project["output_dir"], "publish_jobs": publish_jobs, "scan": report}
    if not asset_ids:
        return {"status": "no_sources", "scan": report}
    candidate_ids = imported_ids or asset_ids
    run_date = datetime.now(timezone.utc).date().isoformat()
    can_reuse_project = not imported_ids and last_project and (
        (last_project.get("status") in {"draft", "failed"} and not last_project.get("output_dir"))
        or (last_project.get("status") == "ready" and not output_exists)
    )
    if can_reuse_project:
        project = last_project
    else:
        audio_tracks = []
        if automation["music_asset_id"]:
            audio_tracks.append({"asset_id": automation["music_asset_id"], "role": "music", "volume": automation["music_volume"], "start_time": 0})
        if automation["sfx_asset_id"]:
            audio_tracks.append({"asset_id": automation["sfx_asset_id"], "role": "sfx", "volume": automation["sfx_volume"], "start_time": automation["sfx_start_time"]})
        if automation["voiceover_asset_id"]:
            audio_tracks.append({"asset_id": automation["voiceover_asset_id"], "role": "voiceover", "volume": automation["voiceover_volume"], "start_time": automation["voiceover_start_time"]})
        project = create_project(
            db,
            {
                "name": f"{automation['name']} · {run_date}",
                "theme": automation["theme"],
                "duration_target": automation["duration_target"],
                "style": automation["style"],
                "title": _template(automation["title_template"], automation, run_date) or automation["name"],
                "caption": _template(automation["caption_template"], automation, run_date) or automation["theme"],
                "audio_tracks": audio_tracks,
            },
        )
    db.update("automations", "id", automation_id, {"last_project_id": project["id"], "updated_at": now_iso()})
    planned = plan_project(db, project["id"], candidate_ids)
    if progress:
        progress(0.45, f"Planned {len(planned['assets'])} production-ready clip{'' if len(planned['assets']) == 1 else 's'}")
    if not planned["assets"]:
        return {"status": "awaiting_review", "project_id": project["id"], "scan": report}
    if not automation["auto_render"]:
        return {"status": "planned", "project_id": project["id"], "scan": report}
    rendered = render_project(db, project["id"], output_dir, lambda value, message: progress(0.45 + value * 0.5, message) if progress else None)
    publish_jobs = _prepare_automation_publish_jobs(db, automation, project["id"], project)
    return {
        "status": "rendered",
        "project_id": project["id"],
        "output_dir": rendered["output_dir"],
        "publish_jobs": publish_jobs,
        "scan": report,
    }
