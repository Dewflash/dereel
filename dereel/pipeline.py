from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .db import Database, json_dumps, json_loads, new_id, now_iso
from .media import add_text_overlay, concat_segments, mix_audio_tracks, normalize_segment, sha256_file, validate_audio, validate_video, write_caption_track


PRODUCTION_RIGHTS = {"owned_distribution", "licensed_distribution"}
AUDIO_ROLES = {"music", "sfx", "voiceover"}


def asset_provenance(row: dict[str, Any]) -> dict[str, Any]:
    platform = str(row.get("platform") or "")
    automation_id = str(row.get("automation_id") or "")
    if row.get("kind") == "reference":
        source_type = "reference"
    elif automation_id:
        source_type = "automation_import"
    elif platform in {"pexels", "pixabay"}:
        source_type = "provider_import"
    else:
        source_type = "local_import"
    return {
        "source_type": source_type,
        "platform": platform,
        "source_url": str(row.get("source_url") or ""),
        "local_path": str(row.get("local_path") or ""),
        "automation_id": automation_id,
        "fingerprint": str(row.get("fingerprint") or ""),
        "captured_at": str(row.get("created_at") or ""),
    }


def review_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    if row.get("kind") == "reference":
        flags.append("reference-only")
    if row.get("kind") == "media" and not row.get("local_path"):
        flags.append("missing-local-file")
    if row.get("rights_status") == "pending_review":
        flags.append("provider-license-review" if row.get("platform") in {"pexels", "pixabay"} else "rights-attestation")
    if row.get("rights_status") == "blocked":
        flags.append("blocked")
    if float(row.get("duration") or 0) > 180:
        flags.append("over-180-seconds")
    return flags


def asset_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["tags"] = json_loads(result.pop("tags_json", "[]"), [])
    result["analysis"] = json_loads(result.pop("analysis_json", "{}"), {})
    result["has_audio"] = bool(result.get("has_audio"))
    result["review_flags"] = review_flags(result)
    result["provenance"] = asset_provenance(result)
    return result


def get_project_audio(db: Database, project_id: str) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT pa.project_id, pa.asset_id, pa.role, pa.volume, pa.start_time,
                   a.title, a.local_path, a.mime_type, a.duration, a.rights_status, a.status,
                   a.kind, a.source_url, a.platform, a.automation_id, a.fingerprint, a.created_at
            FROM project_audio pa JOIN assets a ON a.id = pa.asset_id
            WHERE pa.project_id = ? ORDER BY pa.role, pa.start_time, pa.asset_id
            """,
            (project_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def project_view(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["settings"] = json_loads(result.pop("settings_json", "{}"), {})
    result["manifest"] = json_loads(result.pop("manifest_json", "{}"), {})
    result["assets"] = get_project_assets(db, row["id"])
    result["audio"] = get_project_audio(db, row["id"])
    return result


def get_project_assets(db: Database, project_id: str) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT pa.*, a.title, a.local_path, a.rights_status, a.duration, a.width, a.height, a.tags_json,
                   a.kind, a.source_url, a.platform, a.automation_id, a.fingerprint, a.created_at
            FROM project_assets pa JOIN assets a ON a.id = pa.asset_id
            WHERE pa.project_id = ? ORDER BY pa.order_index
            """,
            (project_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["tags"] = json_loads(item.pop("tags_json", "[]"), [])
        item["enabled"] = bool(item["enabled"])
        result.append(item)
    return result


def update_project_asset(db: Database, project_id: str, asset_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT pa.*, a.duration FROM project_assets pa JOIN assets a ON a.id = pa.asset_id WHERE pa.project_id = ? AND pa.asset_id = ?",
            (project_id, asset_id),
        ).fetchone()
        if not row:
            raise KeyError("Clip is not in this project")
        current = dict(row)
        duration = float(current.get("duration") or 0)
        in_point = max(0.0, float(payload.get("in_point", current["in_point"])))
        out_point_value = payload.get("out_point", current["out_point"])
        out_point = duration if out_point_value in (None, "") else float(out_point_value)
        if in_point >= out_point or out_point > duration + 0.01:
            raise ValueError("Clip trim points must be inside the source duration")
        enabled = int(payload.get("enabled", current["enabled"]))
        if enabled not in (0, 1):
            raise ValueError("Clip enabled state must be 0 or 1")
        order_index = max(0, int(payload.get("order_index", current["order_index"])))
        note = str(payload.get("note", current["note"]))[:240]
        connection.execute(
            "UPDATE project_assets SET in_point = ?, out_point = ?, enabled = ?, order_index = ?, note = ? WHERE project_id = ? AND asset_id = ?",
            (in_point, out_point, enabled, order_index, note, project_id, asset_id),
        )
    return next(item for item in get_project_assets(db, project_id) if item["asset_id"] == asset_id)


def create_asset(
    db: Database,
    *,
    kind: str,
    title: str,
    source_url: str = "",
    platform: str = "",
    local_path: str = "",
    mime_type: str = "",
    tags: list[str] | None = None,
    rights_status: str = "private_reference",
    metadata: dict[str, Any] | None = None,
    automation_id: str = "",
) -> dict[str, Any]:
    if rights_status not in {"private_reference", "owned_distribution", "licensed_distribution", "blocked", "pending_review"}:
        raise ValueError("Unsupported rights status")
    asset_id = new_id("asset")
    now = now_iso()
    details = metadata or {}
    fingerprint = str(details.get("fingerprint") or "")
    if fingerprint and kind == "media":
        with db.connection() as connection:
            duplicate = connection.execute("SELECT id, title FROM assets WHERE kind = 'media' AND fingerprint = ? LIMIT 1", (fingerprint,)).fetchone()
        if duplicate:
            raise ValueError(f"Media matches existing asset '{duplicate['title']}' ({duplicate['id']})")
    db.insert(
        "assets",
        {
            "id": asset_id,
            "kind": kind,
            "title": (title or "Untitled source").strip()[:160],
            "source_url": source_url.strip()[:1000],
            "platform": platform.strip()[:40],
            "local_path": local_path,
            "mime_type": mime_type,
            "duration": details.get("duration"),
            "width": details.get("width"),
            "height": details.get("height"),
            "has_audio": int(bool(details.get("has_audio"))),
            "file_size": details.get("file_size"),
            "fingerprint": fingerprint,
            "automation_id": automation_id,
            "tags_json": json_dumps(sorted({tag.strip().lower() for tag in (tags or []) if tag.strip()})),
            "rights_status": rights_status,
            "status": "candidate" if kind == "reference" else "ready",
            "analysis_json": json_dumps(details),
            "created_at": now,
            "updated_at": now,
        },
    )
    return asset_view(db.get("assets", "id", asset_id) or {})


def analyze_asset(db: Database, asset_id: str) -> dict[str, Any]:
    asset = db.get("assets", "id", asset_id)
    if not asset:
        raise KeyError("Asset not found")
    if not asset.get("local_path"):
        return asset_view(asset)
    path = Path(asset["local_path"])
    metadata = validate_audio(path) if str(asset.get("mime_type") or "").startswith("audio/") else validate_video(path)
    metadata["fingerprint"] = sha256_file(path)
    with db.connection() as connection:
        duplicate = connection.execute("SELECT id, title FROM assets WHERE kind = 'media' AND fingerprint = ? AND id != ? LIMIT 1", (metadata["fingerprint"], asset_id)).fetchone()
    if duplicate:
        raise ValueError(f"Media matches existing asset '{duplicate['title']}' ({duplicate['id']})")
    db.update(
        "assets",
        "id",
        asset_id,
        {
            "duration": metadata["duration"],
            "width": metadata["width"],
            "height": metadata["height"],
            "has_audio": int(metadata["has_audio"]),
            "file_size": metadata["file_size"],
            "fingerprint": metadata["fingerprint"],
            "analysis_json": json_dumps(metadata),
            "updated_at": now_iso(),
        },
    )
    return asset_view(db.get("assets", "id", asset_id) or {})


def list_assets(db: Database, view: str = "all") -> list[dict[str, Any]]:
    query = "SELECT * FROM assets"
    params: tuple[Any, ...] = ()
    if view == "ready":
        query += " WHERE local_path IS NOT NULL AND rights_status IN ('owned_distribution', 'licensed_distribution') AND status IN ('ready', 'approved')"
    elif view == "references":
        query += " WHERE kind = 'reference'"
    elif view == "review":
        query += " WHERE status = 'candidate' OR rights_status = 'pending_review'"
    query += " ORDER BY created_at DESC"
    with db.connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [asset_view(dict(row)) for row in rows]


def bulk_review_assets(db: Database, asset_ids: list[str], action: str) -> list[dict[str, Any]]:
    actions = {
        "approve_owned": ("approved", "owned_distribution"),
        "approve_licensed": ("approved", "licensed_distribution"),
        "reject": ("rejected", "blocked"),
    }
    if action not in actions:
        raise ValueError("Unsupported bulk review action")
    normalized_ids = list(dict.fromkeys(str(asset_id).strip() for asset_id in asset_ids if str(asset_id).strip()))
    if not normalized_ids:
        raise ValueError("Select at least one asset")
    if len(normalized_ids) > 200:
        raise ValueError("Bulk review is limited to 200 assets per action")
    placeholders = ", ".join("?" for _ in normalized_ids)
    with db.connection() as connection:
        rows = connection.execute(f"SELECT * FROM assets WHERE id IN ({placeholders})", normalized_ids).fetchall()
        if len(rows) != len(normalized_ids):
            raise KeyError("One or more selected assets no longer exist")
        invalid = [
            dict(row)
            for row in rows
            if row["kind"] != "media"
            or not row["local_path"]
            or row["rights_status"] not in {"pending_review", "private_reference"}
        ]
        if invalid:
            raise ValueError(f"{len(invalid)} selected asset(s) are not awaiting review")
        status, rights_status = actions[action]
        connection.execute(
            f"UPDATE assets SET status = ?, rights_status = ?, updated_at = ? WHERE id IN ({placeholders})",
            [status, rights_status, now_iso(), *normalized_ids],
        )
        updated = connection.execute(f"SELECT * FROM assets WHERE id IN ({placeholders})", normalized_ids).fetchall()
    by_id = {row["id"]: asset_view(dict(row)) for row in updated}
    return [by_id[asset_id] for asset_id in normalized_ids]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def create_project(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
    project_id = new_id("project")
    now = now_iso()
    name = str(payload.get("name") or "Untitled reel")[:120]
    theme = str(payload.get("theme") or "funny moments")[:180]
    title = str(payload.get("title") or name)[:100]
    caption = str(payload.get("caption") or "")[:500]
    duration_target = max(8, min(float(payload.get("duration_target") or 35), 180))
    style = str(payload.get("style") or "kinetic")[:40]
    add_title_card = _as_bool(payload.get("add_title_card"), True)
    write_captions = _as_bool(payload.get("write_captions"), True)
    audio_tracks = _normalize_audio_tracks(db, payload)
    music_track = next((track for track in audio_tracks if track["role"] == "music"), None)
    db.insert(
        "projects",
        {
            "id": project_id,
            "name": name,
            "theme": theme,
            "format": "vertical_9_16",
            "duration_target": duration_target,
            "style": style,
            "status": "draft",
            "title": title,
            "caption": caption,
            "output_dir": "",
            "settings_json": json_dumps({"clip_gap": 0, "add_title_card": add_title_card, "write_captions": write_captions, "music_asset_id": music_track["asset_id"] if music_track else "", "music_volume": music_track["volume"] if music_track else 0.16, "audio_tracks": audio_tracks}),
            "manifest_json": json_dumps({}),
            "created_at": now,
            "updated_at": now,
        },
    )
    with db.connection() as connection:
        for track in audio_tracks:
            connection.execute(
                "INSERT INTO project_audio(project_id, asset_id, role, volume, start_time) VALUES (?, ?, ?, ?, ?)",
                (project_id, track["asset_id"], track["role"], track["volume"], track["start_time"]),
            )
    return project_view(db, db.get("projects", "id", project_id) or {})


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(minimum, min(parsed, maximum)), 3)


def _normalize_audio_tracks(db: Database, payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tracks = payload.get("audio_tracks")
    if raw_tracks is not None and not isinstance(raw_tracks, list):
        raise ValueError("audio_tracks must be a list")
    candidates: list[dict[str, Any]] = [item for item in raw_tracks if isinstance(item, dict)] if isinstance(raw_tracks, list) else []
    fallback = (
        ("music", "music_asset_id", "music_volume", "music_start_time", 0.16, 0.0),
        ("sfx", "sfx_asset_id", "sfx_volume", "sfx_start_time", 0.55, 0.0),
        ("voiceover", "voiceover_asset_id", "voiceover_volume", "voiceover_start_time", 1.0, 0.0),
    )
    seen = {(str(item.get("asset_id") or ""), str(item.get("role") or "")) for item in candidates}
    for role, asset_key, volume_key, start_key, default_volume, default_start in fallback:
        asset_id = str(payload.get(asset_key) or "").strip()
        if asset_id and (asset_id, role) not in seen:
            candidates.append({"asset_id": asset_id, "role": role, "volume": payload.get(volume_key, default_volume), "start_time": payload.get(start_key, default_start)})
            seen.add((asset_id, role))
    unique_candidates: list[dict[str, Any]] = []
    candidate_keys: set[tuple[str, str]] = set()
    for item in candidates:
        key = (str(item.get("asset_id") or "").strip(), str(item.get("role") or "").strip().lower())
        if key in candidate_keys:
            continue
        candidate_keys.add(key)
        unique_candidates.append(item)
    candidates = unique_candidates
    if len(candidates) > 8:
        raise ValueError("A project can contain at most eight audio tracks")
    tracks: list[dict[str, Any]] = []
    for item in candidates:
        asset_id = str(item.get("asset_id") or "").strip()
        role = str(item.get("role") or "").strip().lower()
        if not asset_id:
            continue
        if role not in AUDIO_ROLES:
            raise ValueError(f"Unsupported audio role: {role or 'empty'}")
        asset = db.get("assets", "id", asset_id)
        if not asset or not asset.get("local_path") or not str(asset.get("mime_type") or "").startswith("audio/"):
            raise ValueError(f"Selected {role} is not a valid audio asset")
        if asset.get("rights_status") not in PRODUCTION_RIGHTS or asset.get("status") not in {"ready", "approved"}:
            raise ValueError(f"Selected {role} is not cleared for production")
        tracks.append({"asset_id": asset_id, "role": role, "volume": _bounded_float(item.get("volume"), 0.16, 0.0, 1.0), "start_time": _bounded_float(item.get("start_time"), 0.0, 0.0, 3600.0)})
    return tracks


def score_breakdown(asset: dict[str, Any], theme_tokens: set[str]) -> dict[str, Any]:
    tags = set(json_loads(asset.get("tags_json"), []))
    title_tokens = set(asset.get("title", "").lower().replace("-", " ").split())
    duration = float(asset.get("duration") or 0)
    theme_matches = sorted(theme_tokens & (tags | title_tokens))
    theme_score = len(theme_matches) * 5
    duration_score = 0
    duration_bucket = "neutral"
    if 2 <= duration <= 12:
        duration_score = 4
        duration_bucket = "short"
    elif duration > 30:
        duration_score = -2
        duration_bucket = "long"
    vertical_score = 0
    vertical = False
    if asset.get("width") and asset.get("height"):
        ratio = asset["width"] / max(asset["height"], 1)
        vertical = ratio < 1.2
        vertical_score = 2 if vertical else 0
    audio_score = 1 if asset.get("has_audio") else 0
    return {
        "total": theme_score + duration_score + vertical_score + audio_score,
        "theme_matches": theme_matches,
        "theme_score": theme_score,
        "duration_bucket": duration_bucket,
        "duration_score": duration_score,
        "vertical": vertical,
        "vertical_score": vertical_score,
        "has_audio": bool(asset.get("has_audio")),
        "audio_score": audio_score,
    }


def score_asset(asset: dict[str, Any], theme_tokens: set[str]) -> float:
    return float(score_breakdown(asset, theme_tokens)["total"])


def selection_reason(breakdown: dict[str, Any]) -> str:
    reasons: list[str] = []
    if breakdown.get("theme_matches"):
        reasons.append("theme: " + ", ".join(breakdown["theme_matches"]))
    if breakdown.get("duration_bucket") == "short":
        reasons.append("short clip")
    elif breakdown.get("duration_bucket") == "long":
        reasons.append("long clip")
    if breakdown.get("vertical"):
        reasons.append("vertical")
    if breakdown.get("has_audio"):
        reasons.append("has audio")
    return f"score {float(breakdown.get('total') or 0):.1f} · " + (" · ".join(reasons) if reasons else "baseline match")


def plan_project(db: Database, project_id: str, candidate_ids: list[str] | None = None) -> dict[str, Any]:
    project = db.get("projects", "id", project_id)
    if not project:
        raise KeyError("Project not found")
    db.update("projects", "id", project_id, {"status": "planning", "updated_at": now_iso()})
    theme_tokens = {token.lower() for token in project["theme"].replace(",", " ").split() if len(token) > 2}
    candidate_filter = ""
    candidate_params: list[str] = []
    if candidate_ids is not None:
        if not candidate_ids:
            rows = []
        else:
            placeholders = ", ".join("?" for _ in candidate_ids)
            candidate_filter = f" AND id IN ({placeholders})"
            candidate_params = candidate_ids
    if candidate_ids is None or candidate_filter:
        with db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM assets WHERE local_path IS NOT NULL AND (mime_type IS NULL OR mime_type NOT LIKE 'audio/%') AND rights_status IN ('owned_distribution', 'licensed_distribution') AND status IN ('ready', 'approved')" + candidate_filter + " ORDER BY created_at DESC",
                candidate_params,
            ).fetchall()
    candidates = [{"asset": dict(row), "breakdown": score_breakdown(dict(row), theme_tokens)} for row in rows]
    candidates.sort(key=lambda item: item["breakdown"]["total"], reverse=True)
    selected: list[dict[str, Any]] = []
    total = 0.0
    target = float(project["duration_target"])
    for candidate_item in candidates:
        if total >= target or len(selected) >= 12:
            break
        candidate = candidate_item["asset"]
        duration = float(candidate.get("duration") or 0)
        if duration <= 0:
            continue
        clip_duration = min(duration, max(1.5, target - total))
        breakdown = candidate_item["breakdown"]
        selected.append({"asset": candidate, "duration": round(clip_duration, 3), "score": breakdown["total"], "breakdown": breakdown, "reason": selection_reason(breakdown)})
        total += clip_duration
    with db.connection() as connection:
        connection.execute("DELETE FROM project_assets WHERE project_id = ?", (project_id,))
        for index, item in enumerate(selected):
            connection.execute(
                "INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, note, enabled, selected_by) VALUES (?, ?, ?, ?, ?, ?, 1, 'auto')",
                (project_id, item["asset"]["id"], index, 0, item["duration"], item["reason"]),
            )
    manifest = {
        "planner": "deterministic-theme-v1",
        "theme": project["theme"],
        "target_duration": target,
        "selected_duration": round(total, 3),
        "candidate_count": len(candidates),
        "selected": [{"asset_id": item["asset"]["id"], "score": item["score"], "duration": item["duration"], "reason": item["reason"], "breakdown": item["breakdown"]} for item in selected],
    }
    db.update(
        "projects",
        "id",
        project_id,
        {"status": "draft", "manifest_json": json_dumps(manifest), "updated_at": now_iso()},
    )
    return project_view(db, db.get("projects", "id", project_id) or {})


def create_job(
    db: Database,
    project_id: str | None,
    kind: str,
    automation_id: str | None = None,
    retry_of: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    job_id = new_id("job")
    now = now_iso()
    db.insert(
        "jobs",
        {"id": job_id, "project_id": project_id, "automation_id": automation_id, "retry_of": retry_of, "attempt": max(1, int(attempt)), "kind": kind, "status": "queued", "progress": 0, "message": "Queued", "error": "", "result_json": "{}", "created_at": now, "updated_at": now},
    )
    return db.get("jobs", "id", job_id) or {}


def retry_failed_job(db: Database, job_id: str) -> dict[str, Any]:
    with db.connection() as connection:
        failed = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not failed:
            raise KeyError("Job not found")
        if failed["status"] != "failed":
            raise ValueError("Only failed jobs can be retried")
        if failed["kind"] not in {"render", "automation"}:
            raise ValueError(f"Job kind '{failed['kind']}' cannot be retried")

        if failed["kind"] == "render":
            subject_column = "project_id"
            subject_id = failed["project_id"]
            if not subject_id:
                raise ValueError("Render job has no project to retry")
            project = connection.execute("SELECT status FROM projects WHERE id = ?", (subject_id,)).fetchone()
            if not project:
                raise ValueError("Render project no longer exists")
            if project["status"] == "ready":
                raise ValueError("Project is already ready; render does not need a retry")
        else:
            subject_column = "automation_id"
            subject_id = failed["automation_id"]
            if not subject_id:
                raise ValueError("Automation job has no recipe to retry")
            automation = connection.execute("SELECT status FROM automations WHERE id = ?", (subject_id,)).fetchone()
            if not automation:
                raise ValueError("Automation recipe no longer exists")
            if automation["status"] in {"queued", "running"}:
                raise ValueError("Automation is already queued or running")

        latest = connection.execute(
            f"SELECT id FROM jobs WHERE kind = ? AND {subject_column} = ? ORDER BY attempt DESC, created_at DESC LIMIT 1",
            (failed["kind"], subject_id),
        ).fetchone()
        if not latest or latest["id"] != failed["id"]:
            raise ValueError("Only the latest failed attempt can be retried")
        active = connection.execute(
            f"SELECT id FROM jobs WHERE kind = ? AND {subject_column} = ? AND status IN ('queued', 'running') LIMIT 1",
            (failed["kind"], subject_id),
        ).fetchone()
        if active:
            raise ValueError("A job for this target is already queued or running")

        if failed["kind"] == "automation":
            connection.execute(
                "UPDATE automations SET status = 'queued', last_error = '', updated_at = ? WHERE id = ?",
                (now_iso(), subject_id),
            )
        retry_id = new_id("job")
        now = now_iso()
        connection.execute(
            "INSERT INTO jobs(id, project_id, automation_id, retry_of, attempt, kind, status, progress, message, error, result_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 'Queued', '', '{}', ?, ?)",
            (retry_id, failed["project_id"], failed["automation_id"], failed["id"], max(1, int(failed["attempt"] or 1)) + 1, failed["kind"], now, now),
        )
        created = connection.execute("SELECT * FROM jobs WHERE id = ?", (retry_id,)).fetchone()
    return dict(created) if created else {}


def set_job(db: Database, job_id: str, **values: Any) -> None:
    values["updated_at"] = now_iso()
    if "result" in values:
        values["result_json"] = json_dumps(values.pop("result"))
    db.update("jobs", "id", job_id, values)


def render_project(db: Database, project_id: str, output_root: Path, progress: Callable[[float, str], None]) -> dict[str, Any]:
    project = db.get("projects", "id", project_id)
    if not project:
        raise KeyError("Project not found")
    project_assets = get_project_assets(db, project_id)
    enabled = [item for item in project_assets if item["enabled"]]
    if not enabled:
        raise ValueError("Add or plan at least one production-ready video before rendering")
    for item in enabled:
        if item["rights_status"] not in PRODUCTION_RIGHTS or not item["local_path"]:
            raise ValueError(f"Asset '{item['title']}' is not cleared for production")
        validate_video(Path(item["local_path"]))

    output_root.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("+00", "Z") + "-" + new_id("run").removeprefix("run_")
    output_dir = output_root / project_id / stamp
    work_dir = Path(tempfile.mkdtemp(prefix=f"{project_id}-", dir=output_root))
    segments: list[Path] = []
    manifest: dict[str, Any] = {
        "project_id": project_id,
        "created_at": now_iso(),
        "format": project["format"],
        "style": project["style"],
        "inputs": [],
        "outputs": {},
    }
    try:
        for index, item in enumerate(enabled):
            source = Path(item["local_path"])
            source_duration = float(item["duration"] or 0)
            start = max(float(item["in_point"] or 0), 0)
            requested = float(item["out_point"] or 0) - start if item["out_point"] else source_duration - start
            duration = min(max(requested, 0.1), source_duration - start)
            segment = work_dir / f"segment-{index:02d}.mp4"
            normalize_segment(source, segment, start, duration)
            segments.append(segment)
            manifest["inputs"].append({"asset_id": item["asset_id"], "title": item["title"], "source": str(source), "provenance": asset_provenance(item), "in": start, "duration": duration})
            progress(0.12 + ((index + 1) / len(enabled)) * 0.56, f"Prepared clip {index + 1} of {len(enabled)}")

        output_dir.mkdir(parents=True, exist_ok=True)
        base = output_dir / "master.mp4"
        concat_segments(segments, base)
        progress(0.78, "Joined the edit")
        settings = json_loads(project.get("settings_json"), {})
        audio_tracks = get_project_audio(db, project_id)
        if not audio_tracks and settings.get("music_asset_id"):
            legacy_music = db.get("assets", "id", str(settings["music_asset_id"]))
            if legacy_music:
                audio_tracks = [{
                    "asset_id": legacy_music["id"],
                    "role": "music",
                    "volume": settings.get("music_volume", 0.16),
                    "start_time": 0,
                    **legacy_music,
                }]
        if audio_tracks:
            mix_inputs: list[dict[str, Any]] = []
            manifest_tracks: list[dict[str, Any]] = []
            for track in audio_tracks:
                if track.get("rights_status") not in PRODUCTION_RIGHTS or track.get("status") not in {"ready", "approved"} or not track.get("local_path") or not str(track.get("mime_type") or "").startswith("audio/"):
                    raise ValueError(f"Selected audio '{track.get('title') or track.get('asset_id')}' is not cleared for production")
                audio_source = Path(track["local_path"])
                validate_audio(audio_source)
                volume = _bounded_float(track.get("volume"), 0.16, 0.0, 1.0)
                start_time = _bounded_float(track.get("start_time"), 0.0, 0.0, 3600.0)
                mix_inputs.append({"source": audio_source, "role": track["role"], "volume": volume, "start_time": start_time})
                manifest_tracks.append({"asset_id": track["asset_id"], "title": track.get("title", ""), "role": track["role"], "source": str(audio_source), "provenance": asset_provenance(track), "volume": volume, "start_time": start_time})
            mixed = output_dir / "mixed.mp4"
            mix_audio_tracks(base, mix_inputs, mixed)
            base.unlink(missing_ok=True)
            base = mixed
            manifest["audio"] = {"tracks": manifest_tracks}
            progress(0.84, f"Mixed {len(mix_inputs)} audio track{'' if len(mix_inputs) == 1 else 's'}")
        add_title_card = _as_bool(settings.get("add_title_card"), True)
        write_captions = _as_bool(settings.get("write_captions"), True)
        titled = output_dir / "final.mp4"
        overlay_applied = add_text_overlay(base, titled, project["title"] or project["name"], project["caption"]) if add_title_card else False
        if not add_title_card:
            shutil.copy2(base, titled)
        base.unlink(missing_ok=True)
        treatment_mode = "drawtext" if overlay_applied else "disabled" if not add_title_card else "unavailable"
        progress(0.9, "Applied title and caption treatment" if overlay_applied else "Rendered without optional text filter")
        for platform in ("youtube", "instagram", "tiktok"):
            destination = output_dir / f"{platform}.mp4"
            shutil.copy2(titled, destination)
            manifest["outputs"][platform] = str(destination)
        manifest["outputs"]["master"] = str(titled)
        manifest["validation"] = validate_render_outputs(output_dir)
        if manifest["validation"]["status"] != "passed":
            raise ValueError("Rendered outputs failed validation: " + "; ".join(manifest["validation"]["problems"]))
        manifest["overlay_applied"] = overlay_applied
        manifest["title_treatment"] = {"requested": add_title_card, "applied": overlay_applied, "mode": treatment_mode}
        caption_path = output_dir / "captions.srt"
        caption_written = write_caption_track(caption_path, project["caption"], sum(item["duration"] for item in manifest["inputs"])) if write_captions else False
        if not write_captions:
            caption_path.unlink(missing_ok=True)
        caption_mode = "static_project_caption" if caption_written else "disabled" if not write_captions else "none"
        manifest["captions"] = {"path": str(caption_path), "written": caption_written, "mode": caption_mode}
        manifest_path = output_dir / "render-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        credit_assets = [item["asset_id"] for item in enabled]
        credit_records = []
        for item in enabled:
            credit_records.append({"asset_id": item["asset_id"], "title": item["title"], "rights_status": item["rights_status"], **asset_provenance(item)})
        for track in audio_tracks:
            credit_assets.append(track["asset_id"])
            credit_records.append({"asset_id": track["asset_id"], "title": track.get("title", ""), "role": track["role"], "rights_status": track["rights_status"], **asset_provenance(track)})
        (output_dir / "credits.json").write_text(json.dumps({"project_id": project_id, "assets": credit_assets, "provenance": credit_records}, indent=2))
        db.update("projects", "id", project_id, {"status": "ready", "output_dir": str(output_dir), "manifest_json": json_dumps(manifest), "updated_at": now_iso()})
        progress(1.0, "Render complete")
        return {"output_dir": str(output_dir), "manifest": manifest}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def validate_render_outputs(output_dir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "passed", "problems": [], "outputs": {}}
    for name in ("master", "youtube", "instagram", "tiktok"):
        path = output_dir / ("final.mp4" if name == "master" else f"{name}.mp4")
        entry: dict[str, Any] = {"path": str(path), "status": "passed", "problems": []}
        try:
            metadata = validate_video(path)
            entry.update({key: metadata[key] for key in ("duration", "width", "height", "has_audio", "file_size")})
            if metadata["width"] != 1080 or metadata["height"] != 1920:
                entry["problems"].append("Expected 1080x1920 vertical output")
        except Exception as error:
            entry["status"] = "failed"
            entry["problems"].append(str(error))
        if entry["problems"]:
            entry["status"] = "failed"
            report["status"] = "failed"
            report["problems"].append(f"{name}: " + "; ".join(entry["problems"]))
        report["outputs"][name] = entry
    return report


def validate_project_output(db: Database, project_id: str) -> dict[str, Any]:
    project = db.get("projects", "id", project_id)
    if not project:
        raise KeyError("Project not found")
    output_dir = Path(project.get("output_dir") or "")
    if not output_dir.is_dir():
        raise ValueError("Project output directory is missing")
    manifest = json_loads(project.get("manifest_json"), {})
    validation = validate_render_outputs(output_dir)
    manifest["validation"] = validation
    status = "ready" if validation["status"] == "passed" else "failed"
    db.update("projects", "id", project_id, {"status": status, "manifest_json": json_dumps(manifest), "updated_at": now_iso()})
    return project_view(db, db.get("projects", "id", project_id) or {})
