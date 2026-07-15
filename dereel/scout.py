from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .db import Database, new_id
from .media import safe_extension, sha256_file, validate_video
from .pipeline import create_asset


PROVIDER_HOSTS = {
    "pexels": {"videos.pexels.com", "images.pexels.com"},
    "pixabay": {"cdn.pixabay.com"},
}


def configured_providers() -> list[dict[str, Any]]:
    return [
        {"id": "pexels", "label": "Pexels", "configured": bool(os.getenv("PEXELS_API_KEY"))},
        {"id": "pixabay", "label": "Pixabay", "configured": bool(os.getenv("PIXABAY_API_KEY"))},
    ]


def _request_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "DeReel/0.1", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Provider returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Provider request failed: {error.reason}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Provider returned an invalid response")
    return payload


def _choose_pexels_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [item for item in files if item.get("link") and item.get("file_type") in {"video/mp4", "video/webm", "video/quicktime"}]
    return max(valid, key=lambda item: (int(item.get("width") or 0), int(item.get("height") or 0)), default=None)


def search_pexels(query: str, per_page: int = 12) -> list[dict[str, Any]]:
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        raise ValueError("PEXELS_API_KEY is not configured")
    params = urllib.parse.urlencode({"query": query[:100], "per_page": max(1, min(per_page, 80))})
    payload = _request_json(f"https://api.pexels.com/videos/search?{params}", {"Authorization": key})
    results = []
    for item in payload.get("videos", []):
        selected = _choose_pexels_file(item.get("video_files", []))
        if not selected:
            continue
        results.append({
            "provider": "pexels",
            "provider_id": str(item.get("id", "")),
            "title": f"Pexels video {item.get('id', '')}",
            "source_url": item.get("url", ""),
            "preview_url": item.get("image", ""),
            "download_url": selected["link"],
            "duration": item.get("duration", 0),
            "width": selected.get("width", 0),
            "height": selected.get("height", 0),
            "license": "Pexels license - verify current terms before publishing",
        })
    return results


def search_pixabay(query: str, per_page: int = 12) -> list[dict[str, Any]]:
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        raise ValueError("PIXABAY_API_KEY is not configured")
    params = urllib.parse.urlencode({"key": key, "q": query[:100], "per_page": max(3, min(per_page, 200)), "video_type": "all", "safesearch": "true"})
    payload = _request_json(f"https://pixabay.com/api/videos/?{params}")
    results = []
    for item in payload.get("hits", []):
        options = item.get("videos", {})
        selected = next((options.get(name) for name in ("large", "medium", "small") if options.get(name, {}).get("url")), None)
        if not selected:
            continue
        results.append({
            "provider": "pixabay",
            "provider_id": str(item.get("id", "")),
            "title": f"Pixabay video {item.get('id', '')}",
            "source_url": item.get("pageURL", ""),
            "preview_url": item.get("picture_id", ""),
            "download_url": selected.get("url", ""),
            "duration": item.get("duration", 0),
            "width": selected.get("width", 0),
            "height": selected.get("height", 0),
            "license": "Pixabay content license - verify current terms before publishing",
        })
    return results


def search(provider: str, query: str, per_page: int = 12) -> list[dict[str, Any]]:
    if not query.strip():
        raise ValueError("Search query is required")
    if provider == "pexels":
        return search_pexels(query, per_page)
    if provider == "pixabay":
        return search_pixabay(query, per_page)
    raise ValueError("Unsupported Scout provider")


def _host_allowed(provider: str, url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in PROVIDER_HOSTS.get(provider, set())


def _provider_asset_exists(db: Database, provider: str, source_url: str, provider_id: str) -> bool:
    with db.connection() as connection:
        if source_url:
            row = connection.execute(
                "SELECT local_path FROM assets WHERE kind = 'media' AND platform = ? AND source_url = ? LIMIT 1",
                (provider, source_url),
            ).fetchone()
            if row and row["local_path"] and Path(row["local_path"]).is_file():
                return True
        if provider_id:
            rows = connection.execute(
                "SELECT local_path, analysis_json FROM assets WHERE kind = 'media' AND platform = ?",
                (provider,),
            ).fetchall()
            for row in rows:
                if not row["local_path"] or not Path(row["local_path"]).is_file():
                    continue
                try:
                    details = json.loads(row["analysis_json"] or "{}")
                except json.JSONDecodeError:
                    details = {}
                if str(details.get("provider_id") or "") == provider_id:
                    return True
    return False


def import_provider_asset(db: Database, destination_dir: Path, payload: dict[str, Any], automation_id: str = "") -> dict[str, Any]:
    provider = str(payload.get("provider") or "")
    download_url = str(payload.get("download_url") or "")
    if not _host_allowed(provider, download_url):
        raise ValueError("Only approved provider media hosts can be imported")
    source_url = str(payload.get("source_url") or "").strip()
    provider_id = str(payload.get("provider_id") or "").strip()
    if _provider_asset_exists(db, provider, source_url, provider_id):
        raise ValueError("Provider import matches existing asset")
    request = urllib.request.Request(download_url, headers={"User-Agent": "DeReel/0.1"})
    destination_dir.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(urllib.parse.urlparse(download_url).path).name or "provider-video")
    asset_file_id = new_id("asset")
    destination: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if not _host_allowed(provider, response.geturl()):
                raise ValueError("Provider redirected the media request to an unapproved host")
            content_type = response.headers.get_content_type()
            destination = destination_dir / f"{asset_file_id}{safe_extension(filename, content_type)}"
            with destination.open("wb") as output:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 800 * 1024 * 1024:
                        raise ValueError("Provider video exceeds the 800 MB limit")
                    output.write(chunk)
        metadata = validate_video(destination)
        metadata["fingerprint"] = sha256_file(destination)
        if provider_id:
            metadata["provider_id"] = provider_id
        return create_asset(
            db,
            kind="media",
            title=str(payload.get("title") or f"{provider.title()} import"),
            source_url=source_url,
            platform=provider,
            local_path=str(destination),
            mime_type=content_type,
            tags=list(payload.get("tags") or []),
            rights_status="pending_review",
            metadata=metadata,
            automation_id=automation_id,
        )
    except Exception:
        if destination:
            destination.unlink(missing_ok=True)
        raise


def import_provider_assets(db: Database, destination_dir: Path, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(payloads, list):
        raise ValueError("Provider import items must be an array")
    if not payloads:
        raise ValueError("Select at least one provider result")
    if len(payloads) > 24:
        raise ValueError("You can import at most 24 provider results at once")
    imported: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            errors.append({"provider": "", "provider_id": "", "title": "", "error": "Provider result must be an object"})
            continue
        try:
            imported.append(import_provider_asset(db, destination_dir, payload))
        except Exception as error:
            errors.append({
                "provider": str(payload.get("provider") or ""),
                "provider_id": str(payload.get("provider_id") or ""),
                "title": str(payload.get("title") or "Provider result"),
                "error": str(error)[:300],
            })
    return {"imported": imported, "errors": errors}
