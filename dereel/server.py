from __future__ import annotations

import json
import mimetypes
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import Settings, settings
from .db import Database, json_dumps, json_loads, new_id, now_iso
from .media import safe_extension, sha256_file, validate_audio, validate_video
from .pipeline import (
    analyze_asset,
    asset_view,
    create_asset,
    create_job,
    create_project,
    get_project_assets,
    list_assets,
    plan_project,
    project_view,
    render_project,
    set_job,
)
from .publisher import create_publish_jobs, publish_job_view
from .scout import configured_providers, import_provider_asset, search as search_provider


MAX_UPLOAD_BYTES = 800 * 1024 * 1024
JOB_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dereel-job")


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
    if not match:
        raise ValueError("Multipart boundary is missing")
    boundary = (match.group(1) or match.group(2)).encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    for raw in body.split(b"--" + boundary):
        raw = raw.strip(b"\r\n-")
        if not raw or b"\r\n\r\n" not in raw:
            continue
        raw_headers, content = raw.split(b"\r\n\r\n", 1)
        headers = raw_headers.decode("utf-8", "replace")
        disposition = re.search(r'name="([^"]+)"(?:; filename="([^"]*)")?', headers, re.IGNORECASE)
        if not disposition:
            continue
        name, filename = disposition.groups()
        content = content.rstrip(b"\r\n")
        content_type_match = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.IGNORECASE)
        part_type = content_type_match.group(1).strip() if content_type_match else "application/octet-stream"
        if filename is not None:
            files[name] = (filename, content, part_type)
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def safe_path(root: Path, relative_or_absolute: str) -> Path:
    root = root.resolve()
    requested = Path(relative_or_absolute)
    candidate = (requested if requested.is_absolute() else root / requested).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Requested path is outside the application directory")
    return candidate


class AppState:
    def __init__(self, app_settings: Settings):
        app_settings.ensure_directories()
        self.settings = app_settings
        self.db = Database(app_settings.database_path)

    def execute_render(self, job_id: str, project_id: str) -> None:
        try:
            self.db.update("projects", "id", project_id, {"status": "rendering", "updated_at": now_iso()})
            set_job(self.db, job_id, status="running", message="Preparing render")

            def progress(value: float, message: str) -> None:
                set_job(self.db, job_id, status="running", progress=round(value, 3), message=message)

            result = render_project(self.db, project_id, self.settings.output_dir, progress)
            set_job(self.db, job_id, status="completed", progress=1, message="Render complete", result=result)
        except Exception as error:
            self.db.update("projects", "id", project_id, {"status": "failed", "updated_at": now_iso()})
            set_job(self.db, job_id, status="failed", message="Render failed", error=str(error))


STATE = AppState(settings)


class Handler(BaseHTTPRequestHandler):
    server_version = "DeReel/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def app(self) -> AppState:
        return STATE

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"error": message}, status)

    def read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid content length") from error
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        return self.rfile.read(length)

    def read_json(self) -> dict[str, Any]:
        body = self.read_body()
        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api_get(parsed.path, parse_qs(parsed.query))
            elif parsed.path.startswith("/media/"):
                self.serve_media(unquote(parsed.path.removeprefix("/media/")))
            else:
                self.serve_static(parsed.path)
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")
                return
            self.handle_api_post(parsed.path)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_PATCH(self) -> None:
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")
                return
            self.handle_api_patch(parsed.path)
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_DELETE(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                self.handle_api_delete(parsed.path)
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Route not found")
        except (ValueError, KeyError) as error:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            self.send_json({"ok": True, "version": "0.1.0", "database": str(self.app.settings.database_path)})
            return
        if path == "/api/dashboard":
            with self.app.db.connection() as connection:
                counts = {
                    "assets": connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
                    "ready_assets": connection.execute("SELECT COUNT(*) FROM assets WHERE local_path IS NOT NULL AND rights_status IN ('owned_distribution', 'licensed_distribution') AND status IN ('ready', 'approved')").fetchone()[0],
                    "projects": connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                    "ready_projects": connection.execute("SELECT COUNT(*) FROM projects WHERE status = 'ready'").fetchone()[0],
                    "publish_queue": connection.execute("SELECT COUNT(*) FROM publish_jobs WHERE status IN ('queued', 'ready')").fetchone()[0],
                }
                recent_projects = [dict(row) for row in connection.execute("SELECT * FROM projects ORDER BY updated_at DESC LIMIT 6").fetchall()]
                jobs = [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 8").fetchall()]
            self.send_json({"counts": counts, "recent_projects": recent_projects, "jobs": jobs})
            return
        if path == "/api/assets":
            self.send_json({"assets": list_assets(self.app.db, (query.get("view") or ["all"])[0])})
            return
        if path == "/api/scout/providers":
            self.send_json({"providers": configured_providers()})
            return
        if path == "/api/scout/search":
            provider = (query.get("provider") or [""])[0]
            search_query = (query.get("q") or [""])[0]
            self.send_json({"provider": provider, "query": search_query, "results": search_provider(provider, search_query)})
            return
        if path == "/api/projects":
            with self.app.db.connection() as connection:
                rows = connection.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
            self.send_json({"projects": [project_view(self.app.db, dict(row)) for row in rows]})
            return
        if path == "/api/jobs":
            with self.app.db.connection() as connection:
                rows = connection.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 30").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["result"] = json_loads(item.pop("result_json", "{}"), {})
                result.append(item)
            self.send_json({"jobs": result})
            return
        if path == "/api/publishing/accounts":
            with self.app.db.connection() as connection:
                rows = connection.execute("SELECT * FROM publishing_accounts ORDER BY created_at DESC").fetchall()
            self.send_json({"accounts": [dict(row) for row in rows]})
            return
        if path == "/api/publishing/jobs":
            with self.app.db.connection() as connection:
                rows = connection.execute("SELECT * FROM publish_jobs ORDER BY updated_at DESC LIMIT 30").fetchall()
            self.send_json({"jobs": [publish_job_view(dict(row)) for row in rows]})
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/stream", path)
        if match:
            asset = self.app.db.get("assets", "id", match.group(1))
            if not asset or not asset.get("local_path"):
                raise FileNotFoundError
            self.serve_file(safe_path(self.app.settings.root, asset["local_path"]))
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/output/([^/]+)", path)
        if match:
            project = self.app.db.get("projects", "id", match.group(1))
            if not project or not project.get("output_dir"):
                raise FileNotFoundError
            output = safe_path(Path(project["output_dir"]), match.group(2))
            self.serve_file(output)
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            project = self.app.db.get("projects", "id", match.group(1))
            if not project:
                raise FileNotFoundError
            self.send_json(project_view(self.app.db, project))
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")

    def handle_api_post(self, path: str) -> None:
        if path == "/api/assets/reference":
            payload = self.read_json()
            asset = create_asset(
                self.app.db,
                kind="reference",
                title=str(payload.get("title") or "Untitled reference"),
                source_url=str(payload.get("source_url") or ""),
                platform=str(payload.get("platform") or ""),
                tags=list(payload.get("tags") or []),
                rights_status="private_reference",
            )
            self.send_json({"asset": asset}, HTTPStatus.CREATED)
            return
        if path == "/api/scout/import":
            asset = import_provider_asset(self.app.db, self.app.settings.originals_dir, self.read_json())
            self.send_json({"asset": asset}, HTTPStatus.CREATED)
            return
        if path == "/api/assets/upload":
            content_type = self.headers.get("Content-Type", "")
            fields, files = parse_multipart(content_type, self.read_body())
            if "file" not in files:
                raise ValueError("A video file is required")
            filename, data, mime_type = files["file"]
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("Video exceeds upload limit")
            asset_id = new_id("asset")
            destination = self.app.settings.originals_dir / f"{asset_id}{safe_extension(filename, mime_type)}"
            destination.write_bytes(data)
            try:
                metadata = validate_video(destination)
                metadata["fingerprint"] = sha256_file(destination)
                asset = create_asset(
                    self.app.db,
                    kind="media",
                    title=fields.get("title") or Path(filename).stem,
                    source_url=fields.get("source_url", ""),
                    platform=fields.get("platform", "local"),
                    local_path=str(destination),
                    mime_type=mime_type,
                    tags=[tag.strip() for tag in fields.get("tags", "").split(",")],
                    rights_status=fields.get("rights_status", "pending_review"),
                    metadata=metadata,
                )
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            self.send_json({"asset": asset}, HTTPStatus.CREATED)
            return
        if path == "/api/assets/audio-upload":
            content_type = self.headers.get("Content-Type", "")
            fields, files = parse_multipart(content_type, self.read_body())
            if "file" not in files:
                raise ValueError("An audio file is required")
            filename, data, mime_type = files["file"]
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("Audio exceeds upload limit")
            asset_id = new_id("asset")
            destination = self.app.settings.audio_dir / f"{asset_id}{safe_extension(filename, mime_type)}"
            destination.write_bytes(data)
            try:
                metadata = validate_audio(destination)
                metadata["fingerprint"] = sha256_file(destination)
                asset = create_asset(
                    self.app.db,
                    kind="media",
                    title=fields.get("title") or Path(filename).stem,
                    source_url=fields.get("source_url", ""),
                    platform=fields.get("platform", "local"),
                    local_path=str(destination),
                    mime_type=mime_type,
                    tags=[tag.strip() for tag in fields.get("tags", "").split(",")],
                    rights_status=fields.get("rights_status", "pending_review"),
                    metadata=metadata,
                )
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            self.send_json({"asset": asset}, HTTPStatus.CREATED)
            return
        if path == "/api/projects":
            self.send_json({"project": create_project(self.app.db, self.read_json())}, HTTPStatus.CREATED)
            return
        if path == "/api/publishing/accounts":
            payload = self.read_json()
            platform = str(payload.get("platform") or "")
            if platform not in {"youtube", "instagram", "tiktok"}:
                raise ValueError("Unsupported publishing platform")
            account_id = new_id("account")
            now = now_iso()
            self.app.db.insert("publishing_accounts", {"id": account_id, "platform": platform, "label": str(payload.get("label") or platform.title()), "status": "not_connected", "config_json": json_dumps({}), "created_at": now, "updated_at": now})
            self.send_json({"account": self.app.db.get("publishing_accounts", "id", account_id)}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/analyze", path)
        if match:
            self.send_json({"asset": analyze_asset(self.app.db, match.group(1))})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/plan", path)
        if match:
            self.send_json({"project": plan_project(self.app.db, match.group(1))})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/render", path)
        if match:
            project_id = match.group(1)
            if not self.app.db.get("projects", "id", project_id):
                raise KeyError("Project not found")
            job = create_job(self.app.db, project_id, "render")
            JOB_POOL.submit(self.app.execute_render, job["id"], project_id)
            self.send_json({"job": job}, HTTPStatus.ACCEPTED)
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/assets", path)
        if match:
            project_id = match.group(1)
            payload = self.read_json()
            asset_id = str(payload.get("asset_id") or "")
            asset = self.app.db.get("assets", "id", asset_id)
            if not asset:
                raise KeyError("Asset not found")
            with self.app.db.connection() as connection:
                order = connection.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM project_assets WHERE project_id = ?", (project_id,)).fetchone()[0]
                connection.execute("INSERT OR REPLACE INTO project_assets(project_id, asset_id, order_index, in_point, out_point, note, enabled, selected_by) VALUES (?, ?, ?, ?, ?, ?, 1, 'manual')", (project_id, asset_id, order, float(payload.get("in_point") or 0), payload.get("out_point"), str(payload.get("note") or "")))
            self.send_json({"project": project_view(self.app.db, self.app.db.get("projects", "id", project_id) or {})})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/publish", path)
        if match:
            project = self.app.db.get("projects", "id", match.group(1))
            if not project:
                raise KeyError("Project not found")
            payload = self.read_json()
            platforms = list(payload.get("platforms") or ["youtube"])
            jobs = create_publish_jobs(self.app.db, project, platforms, payload.get("scheduled_for"))
            self.send_json({"jobs": [publish_job_view(job) for job in jobs]}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/publishing/jobs/([^/]+)/mark-published", path)
        if match:
            job_id = match.group(1)
            job = self.app.db.get("publish_jobs", "id", job_id)
            if not job:
                raise KeyError("Publish job not found")
            payload = self.read_json()
            self.app.db.update("publish_jobs", "id", job_id, {"status": "published", "remote_id": str(payload.get("remote_id") or "manual"), "updated_at": now_iso()})
            self.send_json({"job": publish_job_view(self.app.db.get("publish_jobs", "id", job_id) or {})})
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")

    def handle_api_patch(self, path: str) -> None:
        match = re.fullmatch(r"/api/assets/([^/]+)", path)
        if match:
            asset_id = match.group(1)
            if not self.app.db.get("assets", "id", asset_id):
                raise KeyError("Asset not found")
            payload = self.read_json()
            allowed = {"title", "source_url", "platform", "status", "rights_status"}
            values = {key: payload[key] for key in allowed if key in payload}
            if "status" in values and values["status"] not in {"candidate", "approved", "rejected", "ready"}:
                raise ValueError("Unsupported asset status")
            if "rights_status" in values and values["rights_status"] not in {"private_reference", "owned_distribution", "licensed_distribution", "blocked", "pending_review"}:
                raise ValueError("Unsupported rights status")
            if values:
                values["updated_at"] = now_iso()
                self.app.db.update("assets", "id", asset_id, values)
            self.send_json({"asset": asset_view(self.app.db.get("assets", "id", asset_id) or {})})
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")

    def handle_api_delete(self, path: str) -> None:
        match = re.fullmatch(r"/api/assets/([^/]+)", path)
        if match:
            asset_id = match.group(1)
            asset = self.app.db.get("assets", "id", asset_id)
            if not asset:
                raise KeyError("Asset not found")
            with self.app.db.connection() as connection:
                usage = connection.execute("SELECT COUNT(*) FROM project_assets WHERE asset_id = ?", (asset_id,)).fetchone()[0]
            if usage:
                raise ValueError("Asset is used by a project and cannot be deleted")
            if asset.get("local_path"):
                Path(asset["local_path"]).unlink(missing_ok=True)
            self.app.db.delete("assets", "id", asset_id)
            self.send_json({"deleted": asset_id})
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.removeprefix("/")
        file_path = safe_path(self.app.settings.web_dir, relative)
        self.serve_file(file_path)

    def serve_media(self, relative: str) -> None:
        self.serve_file(safe_path(self.app.settings.originals_dir, relative))

    def serve_file(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)


def serve(app_settings: Settings = settings) -> None:
    global STATE
    STATE = AppState(app_settings)
    httpd = ThreadingHTTPServer((app_settings.host, app_settings.port), Handler)
    print(f"DeReel running at http://{app_settings.host}:{app_settings.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
