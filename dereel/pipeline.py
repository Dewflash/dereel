from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .db import Database, json_dumps, json_loads, new_id, now_iso
from .media import add_text_overlay, concat_segments, mix_audio_bed, normalize_segment, sha256_file, validate_audio, validate_video, write_caption_track


PRODUCTION_RIGHTS = {"owned_distribution", "licensed_distribution"}


def asset_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["tags"] = json_loads(result.pop("tags_json", "[]"), [])
    result["analysis"] = json_loads(result.pop("analysis_json", "{}"), {})
    result["has_audio"] = bool(result.get("has_audio"))
    return result


def project_view(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["settings"] = json_loads(result.pop("settings_json", "{}"), {})
    result["manifest"] = json_loads(result.pop("manifest_json", "{}"), {})
    result["assets"] = get_project_assets(db, row["id"])
    return result


def get_project_assets(db: Database, project_id: str) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT pa.*, a.title, a.local_path, a.rights_status, a.duration, a.width, a.height, a.tags_json
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
) -> dict[str, Any]:
    if rights_status not in {"private_reference", "owned_distribution", "licensed_distribution", "blocked", "pending_review"}:
        raise ValueError("Unsupported rights status")
    asset_id = new_id("asset")
    now = now_iso()
    details = metadata or {}
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
            "fingerprint": details.get("fingerprint", ""),
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


def create_project(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
    project_id = new_id("project")
    now = now_iso()
    name = str(payload.get("name") or "Untitled reel")[:120]
    theme = str(payload.get("theme") or "funny moments")[:180]
    title = str(payload.get("title") or name)[:100]
    caption = str(payload.get("caption") or "")[:500]
    duration_target = max(8, min(float(payload.get("duration_target") or 35), 180))
    style = str(payload.get("style") or "kinetic")[:40]
    music_asset_id = str(payload.get("music_asset_id") or "")
    if music_asset_id:
        music_asset = db.get("assets", "id", music_asset_id)
        if not music_asset or not music_asset.get("local_path") or not str(music_asset.get("mime_type") or "").startswith("audio/"):
            raise ValueError("Selected music is not a valid audio asset")
        if music_asset.get("rights_status") not in PRODUCTION_RIGHTS or music_asset.get("status") not in {"ready", "approved"}:
            raise ValueError("Selected music is not cleared for production")
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
            "settings_json": json_dumps({"clip_gap": 0, "add_title_card": True, "music_asset_id": music_asset_id, "music_volume": max(0, min(float(payload.get("music_volume") or 0.16), 1))}),
            "manifest_json": json_dumps({}),
            "created_at": now,
            "updated_at": now,
        },
    )
    return project_view(db, db.get("projects", "id", project_id) or {})


def score_asset(asset: dict[str, Any], theme_tokens: set[str]) -> float:
    tags = set(json_loads(asset.get("tags_json"), []))
    title_tokens = set(asset.get("title", "").lower().replace("-", " ").split())
    duration = float(asset.get("duration") or 0)
    score = 0.0
    score += len(theme_tokens & (tags | title_tokens)) * 5
    if 2 <= duration <= 12:
        score += 4
    elif duration > 30:
        score -= 2
    if asset.get("width") and asset.get("height"):
        ratio = asset["width"] / max(asset["height"], 1)
        score += 2 if ratio < 1.2 else 0
    if asset.get("has_audio"):
        score += 1
    return score


def plan_project(db: Database, project_id: str) -> dict[str, Any]:
    project = db.get("projects", "id", project_id)
    if not project:
        raise KeyError("Project not found")
    db.update("projects", "id", project_id, {"status": "planning", "updated_at": now_iso()})
    theme_tokens = {token.lower() for token in project["theme"].replace(",", " ").split() if len(token) > 2}
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM assets WHERE local_path IS NOT NULL AND (mime_type IS NULL OR mime_type NOT LIKE 'audio/%') AND rights_status IN ('owned_distribution', 'licensed_distribution') AND status IN ('ready', 'approved') ORDER BY created_at DESC"
        ).fetchall()
    candidates = sorted((dict(row) for row in rows), key=lambda item: score_asset(item, theme_tokens), reverse=True)
    selected: list[dict[str, Any]] = []
    total = 0.0
    target = float(project["duration_target"])
    for candidate in candidates:
        if total >= target or len(selected) >= 12:
            break
        duration = float(candidate.get("duration") or 0)
        if duration <= 0:
            continue
        clip_duration = min(duration, max(1.5, target - total))
        selected.append({"asset": candidate, "duration": round(clip_duration, 3), "score": score_asset(candidate, theme_tokens)})
        total += clip_duration
    with db.connection() as connection:
        connection.execute("DELETE FROM project_assets WHERE project_id = ?", (project_id,))
        for index, item in enumerate(selected):
            connection.execute(
                "INSERT INTO project_assets(project_id, asset_id, order_index, in_point, out_point, note, enabled, selected_by) VALUES (?, ?, ?, ?, ?, ?, 1, 'auto')",
                (project_id, item["asset"]["id"], index, 0, item["duration"], f"Theme match {item['score']:.1f}"),
            )
    manifest = {
        "planner": "deterministic-theme-v1",
        "theme": project["theme"],
        "target_duration": target,
        "selected_duration": round(total, 3),
        "candidate_count": len(candidates),
        "selected": [{"asset_id": item["asset"]["id"], "score": item["score"], "duration": item["duration"]} for item in selected],
    }
    db.update(
        "projects",
        "id",
        project_id,
        {"status": "draft", "manifest_json": json_dumps(manifest), "updated_at": now_iso()},
    )
    return project_view(db, db.get("projects", "id", project_id) or {})


def create_job(db: Database, project_id: str | None, kind: str) -> dict[str, Any]:
    job_id = new_id("job")
    now = now_iso()
    db.insert(
        "jobs",
        {"id": job_id, "project_id": project_id, "kind": kind, "status": "queued", "progress": 0, "message": "Queued", "error": "", "result_json": "{}", "created_at": now, "updated_at": now},
    )
    return db.get("jobs", "id", job_id) or {}


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
            manifest["inputs"].append({"asset_id": item["asset_id"], "title": item["title"], "source": str(source), "in": start, "duration": duration})
            progress(0.12 + ((index + 1) / len(enabled)) * 0.56, f"Prepared clip {index + 1} of {len(enabled)}")

        output_dir.mkdir(parents=True, exist_ok=True)
        base = output_dir / "master.mp4"
        concat_segments(segments, base)
        progress(0.78, "Joined the edit")
        settings = json_loads(project.get("settings_json"), {})
        music_asset_id = settings.get("music_asset_id")
        if music_asset_id:
            audio_asset = db.get("assets", "id", music_asset_id)
            if not audio_asset or audio_asset.get("rights_status") not in PRODUCTION_RIGHTS or not audio_asset.get("local_path"):
                raise ValueError("Selected music is not cleared for production")
            audio_source = Path(audio_asset["local_path"])
            validate_audio(audio_source)
            mixed = output_dir / "mixed.mp4"
            mix_audio_bed(base, audio_source, mixed, float(settings.get("music_volume", 0.16)))
            base.unlink(missing_ok=True)
            base = mixed
            manifest["audio"] = {"asset_id": music_asset_id, "source": str(audio_source), "volume": float(settings.get("music_volume", 0.16))}
            progress(0.84, "Mixed the audio bed")
        titled = output_dir / "final.mp4"
        overlay_applied = add_text_overlay(base, titled, project["title"] or project["name"], project["caption"])
        base.unlink(missing_ok=True)
        progress(0.9, "Applied title and caption treatment" if overlay_applied else "Rendered without optional text filter")
        for platform in ("youtube", "instagram", "tiktok"):
            destination = output_dir / f"{platform}.mp4"
            shutil.copy2(titled, destination)
            manifest["outputs"][platform] = str(destination)
        manifest["outputs"]["master"] = str(titled)
        manifest["overlay_applied"] = overlay_applied
        caption_path = output_dir / "captions.srt"
        caption_written = write_caption_track(caption_path, project["caption"], sum(item["duration"] for item in manifest["inputs"]))
        manifest["captions"] = {"path": str(caption_path), "written": caption_written, "mode": "static_project_caption" if caption_written else "none"}
        manifest_path = output_dir / "render-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        credit_assets = [item["asset_id"] for item in enabled]
        if music_asset_id:
            credit_assets.append(music_asset_id)
        (output_dir / "credits.json").write_text(json.dumps({"assets": credit_assets}, indent=2))
        db.update("projects", "id", project_id, {"status": "ready", "output_dir": str(output_dir), "manifest_json": json_dumps(manifest), "updated_at": now_iso()})
        progress(1.0, "Render complete")
        return {"output_dir": str(output_dir), "manifest": manifest}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
