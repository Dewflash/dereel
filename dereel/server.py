from __future__ import annotations

import json
import mimetypes
import re
import shutil
import threading
import tempfile
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import parse_qs, unquote, urlparse

from .config import Settings, settings
from .db import Database, json_dumps, json_loads, new_id, now_iso
from .automation import (
    claim_automation,
    create_automation,
    delete_automation,
    due_automations,
    finish_automation,
    create_automation_run,
    finish_automation_run,
    list_automations,
    list_automation_runs,
    run_automation,
    set_automation_running,
    update_automation,
)
from .media import available_filters, pillow_text_overlay_available, safe_extension, sha256_file, validate_audio, validate_video
from .pipeline import (
    add_project_asset,
    analyze_asset,
    audit_asset_health,
    asset_view,
    bulk_review_assets,
    bulk_review_content_assets,
    create_asset,
    create_job,
    create_project,
    check_asset_health,
    delete_project,
    get_project_assets,
    list_content_reviews,
    list_asset_reviews,
    list_assets,
    move_project_asset,
    plan_project,
    remove_project_asset,
    retry_failed_job,
    project_view,
    render_project,
    review_asset,
    review_content_asset,
    set_job,
    update_project_asset,
    update_project,
    validate_project_output,
)
from .publisher import account_view, backfill_publish_integrity, begin_publish_attempt, cancel_publish_job, check_publishing_account, claim_publish_job, complete_publish_attempt, complete_publish_job, create_publish_jobs, delete_publishing_account, fail_publish_attempt, force_publish_success, list_publish_attempts, mark_publish_job, platform_capabilities, publish_job, publish_job_view, publish_result_status, reschedule_publish_job, update_publishing_account
from .scout import configured_providers, import_provider_asset, import_provider_assets, search as search_provider


MAX_UPLOAD_BYTES = 800 * 1024 * 1024
FILE_STREAM_CHUNK_SIZE = 1024 * 1024
MULTIPART_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
MAX_MULTIPART_HEADER_BYTES = 64 * 1024
JOB_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dereel-job")
INTERRUPTED_JOB_ERROR = "Interrupted by process restart"


def finalize_publish_failure(db: Database, job_id: str, attempt: dict[str, Any] | None, error: Exception | str) -> None:
    message = str(error)[:1000]
    try:
        if attempt:
            complete_publish_attempt(db, attempt["id"], "failed", error=message)
        else:
            complete_publish_job(db, job_id, "failed", error=message)
    except Exception as finalization_error:
        try:
            fail_publish_attempt(db, job_id, attempt["id"] if attempt else "", f"{message}; finalization fallback: {finalization_error}")
        except Exception:
            return


def finalize_publish_success(db: Database, job_id: str, attempt: dict[str, Any], remote_id: str) -> None:
    status = publish_result_status(remote_id)
    try:
        complete_publish_attempt(db, attempt["id"], status, remote_id=remote_id)
    except Exception:
        try:
            force_publish_success(db, job_id, attempt["id"], remote_id, status=status)
        except Exception:
            return


def runtime_diagnostics(state: Any | None = None) -> dict[str, Any]:
    ffmpeg_path = shutil.which("ffmpeg") or ""
    ffprobe_path = shutil.which("ffprobe") or ""
    filters = available_filters() if ffmpeg_path else set()
    pillow_overlay = "overlay" in filters and pillow_text_overlay_available()
    diagnostics: dict[str, Any] = {
        "media": {
            "ffmpeg": bool(ffmpeg_path),
            "ffprobe": bool(ffprobe_path),
            "drawtext": "drawtext" in filters,
            "pillow": pillow_overlay,
            "text_overlay": "drawtext" in filters or pillow_overlay,
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_path": ffprobe_path,
        },
        "providers": configured_providers(),
    }
    if state is not None:
        diagnostics["scheduler"] = state.scheduler_diagnostics()
    return diagnostics


def recover_interrupted_jobs(db: Database) -> list[str]:
    recovered: list[str] = []
    timestamp = now_iso()
    with db.connection() as connection:
        connection.execute(
            "UPDATE automations SET status = 'idle', updated_at = ? WHERE status IN ('queued', 'running')",
            (timestamp,),
        )
        rows = connection.execute("SELECT id, kind, project_id, automation_id FROM jobs WHERE status IN ('queued', 'running')").fetchall()
        for row in rows:
            connection.execute(
                "UPDATE jobs SET status = 'failed', message = ?, error = ?, updated_at = ? WHERE id = ?",
                ("Job interrupted", INTERRUPTED_JOB_ERROR, timestamp, row["id"]),
            )
            if row["kind"] == "render" and row["project_id"]:
                connection.execute(
                    "UPDATE projects SET status = 'failed', updated_at = ? WHERE id = ? AND status IN ('planning', 'rendering')",
                    (timestamp, row["project_id"]),
                )
            if row["kind"] == "automation" and row["automation_id"]:
                connection.execute(
                    "UPDATE automations SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                    (INTERRUPTED_JOB_ERROR, timestamp, row["automation_id"]),
                )
                connection.execute(
                    "UPDATE automation_runs SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE job_id = ? AND status IN ('queued', 'running')",
                    (INTERRUPTED_JOB_ERROR, timestamp, timestamp, row["id"]),
                )
            recovered.append(str(row["id"]))
        connection.execute(
            "UPDATE publish_attempts SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE status = 'running'",
            (INTERRUPTED_JOB_ERROR, timestamp, timestamp),
        )
        connection.execute(
            "UPDATE publish_jobs SET status = 'failed', error = ?, claim_token = NULL, claimed_at = NULL, updated_at = ? WHERE claim_token IS NOT NULL AND claim_token != ''",
            (INTERRUPTED_JOB_ERROR, timestamp),
        )
    return recovered


def _multipart_boundary(content_type: str) -> bytes:
    match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type)
    if not match:
        raise ValueError("Multipart boundary is missing")
    return (match.group(1) or match.group(2)).strip().encode()


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    boundary = _multipart_boundary(content_type)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    delimiter = b"--" + boundary
    for raw in body.split(delimiter)[1:]:
        if raw.startswith(b"--"):
            break
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        if not raw or b"\r\n\r\n" not in raw:
            continue
        raw_headers, content = raw.split(b"\r\n\r\n", 1)
        headers = raw_headers.decode("utf-8", "replace")
        disposition = re.search(r'name="([^"]+)"(?:; filename="([^"]*)")?', headers, re.IGNORECASE)
        if not disposition:
            continue
        name, filename = disposition.groups()
        content_type_match = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.IGNORECASE)
        part_type = content_type_match.group(1).strip() if content_type_match else "application/octet-stream"
        if filename is not None:
            files[name] = (filename, content, part_type)
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


class _MultipartCursor:
    def __init__(self, stream: BinaryIO, length: int):
        self.stream = stream
        self.remaining = max(0, length)
        self.buffer = bytearray()

    def _read_source(self, size: int) -> bytes:
        if self.remaining <= 0:
            return b""
        chunk = self.stream.read(min(size, self.remaining))
        if not chunk:
            self.remaining = 0
            return b""
        self.remaining -= len(chunk)
        return chunk

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b""
        output = bytearray()
        if self.buffer:
            take = min(size, len(self.buffer))
            output.extend(self.buffer[:take])
            del self.buffer[:take]
        if len(output) < size:
            output.extend(self._read_source(size - len(output)))
        return bytes(output)

    def push_front(self, data: bytes) -> None:
        if data:
            self.buffer[:0] = data

    def readline(self, limit: int = MAX_MULTIPART_HEADER_BYTES) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                end = newline + 1
                line = bytes(self.buffer[:end])
                del self.buffer[:end]
                if len(line) > limit:
                    raise ValueError("Multipart header is too long")
                return line
            if len(self.buffer) >= limit:
                raise ValueError("Multipart header is too long")
            chunk = self._read_source(min(FILE_STREAM_CHUNK_SIZE, limit - len(self.buffer)))
            if not chunk:
                return bytes(self.buffer)
            self.buffer.extend(chunk)


def _stream_multipart_part(cursor: _MultipartCursor, destination: Any, boundary: bytes) -> bool:
    marker = b"\r\n--" + boundary
    keep = len(marker) + 2
    pending = bytearray()
    while True:
        chunk = cursor.read(FILE_STREAM_CHUNK_SIZE)
        if chunk:
            pending.extend(chunk)
        while True:
            index = pending.find(marker)
            if index < 0:
                if len(pending) > keep:
                    flush_length = len(pending) - keep
                    destination.write(pending[:flush_length])
                    del pending[:flush_length]
                break
            suffix_start = index + len(marker)
            while len(pending) < suffix_start + 2:
                extra = cursor.read(FILE_STREAM_CHUNK_SIZE)
                if not extra:
                    raise ValueError("Multipart body ended before its boundary")
                pending.extend(extra)
            suffix = bytes(pending[suffix_start:suffix_start + 2])
            if suffix not in {b"\r\n", b"--"}:
                destination.write(pending[:index + 1])
                del pending[:index + 1]
                continue
            destination.write(pending[:index])
            remainder = bytes(pending[suffix_start + 2:])
            cursor.push_front(remainder)
            return suffix == b"--"
        if not chunk:
            raise ValueError("Multipart body ended before its boundary")


def parse_multipart_stream(
    content_type: str,
    stream: BinaryIO,
    length: int,
) -> tuple[dict[str, str], dict[str, tuple[str, Any, str]]]:
    boundary = _multipart_boundary(content_type)
    cursor = _MultipartCursor(stream, length)
    opening = b"--" + boundary
    first_line = cursor.readline()
    if first_line == opening + b"--\r\n":
        return {}, {}
    if first_line != opening + b"\r\n":
        raise ValueError("Multipart body has an invalid opening boundary")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, Any, str]] = {}
    try:
        while True:
            header_lines: list[bytes] = []
            while True:
                line = cursor.readline()
                if not line:
                    raise ValueError("Multipart body ended inside part headers")
                if line == b"\r\n":
                    break
                header_lines.append(line)
            headers = b"".join(header_lines).decode("utf-8", "replace")
            disposition = re.search(r'name="([^"]+)"(?:; filename="([^"]*)")?', headers, re.IGNORECASE)
            content_type_match = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.IGNORECASE)
            part_type = content_type_match.group(1).strip() if content_type_match else "application/octet-stream"
            part = tempfile.SpooledTemporaryFile(max_size=MULTIPART_SPOOL_MEMORY_BYTES, mode="w+b")
            try:
                is_final = _stream_multipart_part(cursor, part, boundary)
            except Exception:
                part.close()
                raise
            part.seek(0)
            if disposition:
                name, filename = disposition.groups()
                if filename is not None:
                    files[name] = (filename, part, part_type)
                else:
                    fields[name] = part.read().decode("utf-8", "replace")
                    part.close()
            else:
                part.close()
            if is_final:
                return fields, files
    except Exception:
        for _, (_, part, _) in files.items():
            part.close()
        raise


def close_multipart_files(files: dict[str, tuple[str, Any, str]]) -> None:
    for _, (_, part, _) in files.items():
        part.close()


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
        self.stop_event = threading.Event()
        self._scheduler_status_lock = threading.Lock()
        self._scheduler_status: dict[str, Any] = {
            "status": "starting",
            "last_run_at": None,
            "last_success_at": None,
            "last_error": "",
            "last_error_at": None,
        }
        recover_interrupted_jobs(self.db)
        backfill_publish_integrity(self.db)

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

    def submit_render(self, job: dict[str, Any], project_id: str) -> dict[str, Any]:
        try:
            JOB_POOL.submit(self.execute_render, job["id"], project_id)
        except Exception as error:
            self.db.update("projects", "id", project_id, {"status": "failed", "updated_at": now_iso()})
            set_job(self.db, job["id"], status="failed", message="Render could not start", error=str(error))
        return self.db.get("jobs", "id", job["id"]) or job

    def queue_render(self, project_id: str) -> dict[str, Any]:
        project = self.db.get("projects", "id", project_id)
        if not project:
            raise KeyError("Project not found")
        if project["status"] in {"planning", "rendering"}:
            raise ValueError("Project is already processing")
        with self.db.connection() as connection:
            active = connection.execute(
                "SELECT id FROM jobs WHERE kind = 'render' AND project_id = ? AND status IN ('queued', 'running') LIMIT 1",
                (project_id,),
            ).fetchone()
        if active:
            raise ValueError("A render for this project is already queued or running")
        return self.submit_render(create_job(self.db, project_id, "render"), project_id)

    def execute_automation(self, job_id: str, automation_id: str) -> None:
        run_id = ""
        try:
            set_automation_running(self.db, automation_id)
            set_job(self.db, job_id, status="running", message="Scanning watch folder")
            run_id = create_automation_run(self.db, automation_id, job_id)["id"]

            def progress(value: float, message: str) -> None:
                set_job(self.db, job_id, status="running", progress=round(value, 3), message=message)

            result = run_automation(
                self.db,
                automation_id,
                self.settings.originals_dir,
                self.settings.output_dir,
                progress,
            )
            finish_automation_run(self.db, run_id, result=result)
            finish_automation(self.db, automation_id, result=result)
            if result.get("project_id"):
                self.db.update("jobs", "id", job_id, {"project_id": result["project_id"]})
            set_job(self.db, job_id, status="completed", progress=1, message=f"Automation {result.get('status', 'complete')}", result={**result, "run_id": run_id})
        except Exception as error:
            if run_id:
                finish_automation_run(self.db, run_id, error=str(error))
            finish_automation(self.db, automation_id, error=str(error))
            set_job(self.db, job_id, status="failed", message="Automation failed", error=str(error))

    def submit_automation(self, job: dict[str, Any], automation_id: str) -> dict[str, Any]:
        try:
            JOB_POOL.submit(self.execute_automation, job["id"], automation_id)
        except Exception as error:
            finish_automation(self.db, automation_id, error=str(error))
            set_job(self.db, job["id"], status="failed", message="Automation could not start", error=str(error))
        return self.db.get("jobs", "id", job["id"]) or job

    def retry_job(self, job_id: str) -> dict[str, Any]:
        job = retry_failed_job(self.db, job_id)
        if job["kind"] == "render":
            return self.submit_render(job, job["project_id"])
        return self.submit_automation(job, job["automation_id"])

    def queue_automation(self, automation_id: str) -> dict[str, Any]:
        if not self.db.get("automations", "id", automation_id):
            raise KeyError("Automation not found")
        if not claim_automation(self.db, automation_id):
            raise ValueError("Automation is already running")
        job = create_job(self.db, None, "automation", automation_id)
        return self.submit_automation(job, automation_id)

    def queue_due_automations(self) -> None:
        for automation in due_automations(self.db):
            if claim_automation(self.db, automation["id"]):
                job = create_job(self.db, None, "automation", automation["id"])
                self.submit_automation(job, automation["id"])

    def scheduler_diagnostics(self) -> dict[str, Any]:
        with self._scheduler_status_lock:
            return dict(self._scheduler_status)

    def _update_scheduler_status(self, **values: Any) -> None:
        with self._scheduler_status_lock:
            self._scheduler_status.update(values)

    def run_scheduler_iteration(self) -> None:
        self.queue_due_automations()
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM publish_jobs WHERE status = 'ready' AND scheduled_for IS NOT NULL AND scheduled_for != ''").fetchall()
        now = datetime.now(timezone.utc)
        for row in rows:
            scheduled_for = row["scheduled_for"]
            try:
                due_at = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
            except ValueError:
                self.db.update("publish_jobs", "id", row["id"], {"status": "failed", "error": "Invalid scheduled_for timestamp", "claim_token": None, "claimed_at": None, "updated_at": now_iso()})
                continue
            if due_at > now:
                continue
            if not claim_publish_job(self.db, row["id"]):
                continue
            claimed = self.db.get("publish_jobs", "id", row["id"]) or dict(row)
            attempt: dict[str, Any] | None = None
            try:
                attempt = begin_publish_attempt(self.db, row["id"], "scheduler")
                remote_id = publish_job(self.db, claimed)
                finalize_publish_success(self.db, row["id"], attempt, remote_id)
            except Exception as error:
                finalize_publish_failure(self.db, row["id"], attempt, error)

    def scheduler_loop(self) -> None:
        while not self.stop_event.wait(15):
            self._update_scheduler_status(status="running", last_run_at=now_iso())
            try:
                self.run_scheduler_iteration()
            except Exception as error:
                self._update_scheduler_status(status="degraded", last_error=str(error)[:1000], last_error_at=now_iso())
            else:
                self._update_scheduler_status(status="ok", last_success_at=now_iso())


STATE: AppState | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "DeReel/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def app(self) -> AppState:
        global STATE
        if STATE is None:
            STATE = AppState(settings)
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
        if length < 0:
            raise ValueError("Invalid content length")
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        return self.rfile.read(length)

    def read_multipart(self) -> tuple[dict[str, str], dict[str, tuple[str, Any, str]]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid content length") from error
        if length < 0:
            raise ValueError("Invalid content length")
        if length > MAX_UPLOAD_BYTES:
            raise ValueError(f"Upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
        return parse_multipart_stream(self.headers.get("Content-Type", ""), self.rfile, length)

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
        if path == "/api/diagnostics":
            self.send_json(runtime_diagnostics(self.app))
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
        if path == "/api/automations":
            self.send_json({"automations": list_automations(self.app.db)})
            return
        match = re.fullmatch(r"/api/automations/([^/]+)/runs", path)
        if match:
            if not self.app.db.get("automations", "id", match.group(1)):
                raise KeyError("Automation not found")
            self.send_json({"runs": list_automation_runs(self.app.db, match.group(1))})
            return
        if path == "/api/publishing/accounts":
            with self.app.db.connection() as connection:
                rows = connection.execute("SELECT * FROM publishing_accounts ORDER BY created_at DESC").fetchall()
            self.send_json({"accounts": [account_view(dict(row)) for row in rows]})
            return
        if path == "/api/publishing/platforms":
            self.send_json({"platforms": platform_capabilities()})
            return
        if path == "/api/publishing/jobs":
            with self.app.db.connection() as connection:
                rows = connection.execute("SELECT * FROM publish_jobs ORDER BY updated_at DESC LIMIT 30").fetchall()
            self.send_json({"jobs": [publish_job_view(dict(row)) for row in rows]})
            return
        match = re.fullmatch(r"/api/publishing/jobs/([^/]+)/attempts", path)
        if match:
            if not self.app.db.get("publish_jobs", "id", match.group(1)):
                raise KeyError("Publish job not found")
            self.send_json({"attempts": list_publish_attempts(self.app.db, match.group(1))})
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/stream", path)
        if match:
            asset = self.app.db.get("assets", "id", match.group(1))
            if not asset or not asset.get("local_path"):
                raise FileNotFoundError
            self.serve_file(safe_path(self.app.settings.root, asset["local_path"]))
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/reviews", path)
        if match:
            if not self.app.db.get("assets", "id", match.group(1)):
                raise KeyError("Asset not found")
            self.send_json({"reviews": list_asset_reviews(self.app.db, match.group(1))})
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/content-reviews", path)
        if match:
            if not self.app.db.get("assets", "id", match.group(1)):
                raise KeyError("Asset not found")
            self.send_json({"reviews": list_content_reviews(self.app.db, match.group(1))})
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
        if path == "/api/scout/import/bulk":
            payload = self.read_json()
            result = import_provider_assets(self.app.db, self.app.settings.originals_dir, payload.get("items"))
            self.send_json(result)
            return
        if path == "/api/assets/upload":
            fields, files = self.read_multipart()
            if "file" not in files:
                close_multipart_files(files)
                raise ValueError("A video file is required")
            requested_rights = fields.get("rights_status", "pending_review")
            if requested_rights not in {"pending_review", "owned_distribution", "licensed_distribution"}:
                close_multipart_files(files)
                raise ValueError("Unsupported import rights status")
            filename, data, mime_type = files["file"]
            asset_id = new_id("asset")
            destination = self.app.settings.originals_dir / f"{asset_id}{safe_extension(filename, mime_type)}"
            created_asset_id = ""
            try:
                with destination.open("wb") as output:
                    shutil.copyfileobj(data, output, FILE_STREAM_CHUNK_SIZE)
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
                    rights_status="pending_review",
                    metadata=metadata,
                )
                created_asset_id = asset["id"]
                if requested_rights != "pending_review":
                    action = "approve_owned" if requested_rights == "owned_distribution" else "approve_licensed"
                    asset = review_asset(self.app.db, asset["id"], action, f"Upload attestation: {requested_rights.replace('_', ' ')}")
            except Exception:
                if created_asset_id:
                    self.app.db.delete("assets", "id", created_asset_id)
                destination.unlink(missing_ok=True)
                raise
            finally:
                close_multipart_files(files)
            self.send_json({"asset": asset}, HTTPStatus.CREATED)
            return
        if path == "/api/assets/audio-upload":
            fields, files = self.read_multipart()
            if "file" not in files:
                close_multipart_files(files)
                raise ValueError("An audio file is required")
            requested_rights = fields.get("rights_status", "pending_review")
            if requested_rights not in {"pending_review", "owned_distribution", "licensed_distribution"}:
                close_multipart_files(files)
                raise ValueError("Unsupported import rights status")
            filename, data, mime_type = files["file"]
            asset_id = new_id("asset")
            destination = self.app.settings.audio_dir / f"{asset_id}{safe_extension(filename, mime_type)}"
            created_asset_id = ""
            try:
                with destination.open("wb") as output:
                    shutil.copyfileobj(data, output, FILE_STREAM_CHUNK_SIZE)
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
                    rights_status="pending_review",
                    metadata=metadata,
                )
                created_asset_id = asset["id"]
                if requested_rights != "pending_review":
                    action = "approve_owned" if requested_rights == "owned_distribution" else "approve_licensed"
                    asset = review_asset(self.app.db, asset["id"], action, f"Audio upload attestation: {requested_rights.replace('_', ' ')}")
            except Exception:
                if created_asset_id:
                    self.app.db.delete("assets", "id", created_asset_id)
                destination.unlink(missing_ok=True)
                raise
            finally:
                close_multipart_files(files)
            self.send_json({"asset": asset}, HTTPStatus.CREATED)
            return
        if path == "/api/projects":
            self.send_json({"project": create_project(self.app.db, self.read_json())}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/validate-output", path)
        if match:
            self.send_json({"project": validate_project_output(self.app.db, match.group(1))})
            return
        if path == "/api/automations":
            self.send_json({"automation": create_automation(self.app.db, self.read_json())}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/automations/([^/]+)/run", path)
        if match:
            self.send_json({"job": self.app.queue_automation(match.group(1))}, HTTPStatus.ACCEPTED)
            return
        if path == "/api/publishing/accounts":
            payload = self.read_json()
            platform = str(payload.get("platform") or "")
            if platform not in {"youtube", "instagram", "tiktok"}:
                raise ValueError("Unsupported publishing platform")
            config = {}
            if platform == "instagram":
                config = {
                    "instagram_user_id": str(payload.get("instagram_user_id") or "").strip(),
                    "public_media_base_url": str(payload.get("public_media_base_url") or "").strip().rstrip("/"),
                }
            account_id = new_id("account")
            now = now_iso()
            self.app.db.insert("publishing_accounts", {"id": account_id, "platform": platform, "label": str(payload.get("label") or platform.title()), "status": "not_connected", "config_json": json_dumps(config), "created_at": now, "updated_at": now})
            self.send_json({"account": self.app.db.get("publishing_accounts", "id", account_id)}, HTTPStatus.CREATED)
            return
        if path == "/api/assets/health-check":
            payload = self.read_json()
            asset_ids = payload.get("ids")
            if asset_ids is not None and not isinstance(asset_ids, list):
                raise ValueError("ids must be a list")
            if asset_ids == []:
                raise ValueError("Select at least one asset, or omit ids to check the library")
            self.send_json({"report": audit_asset_health(self.app.db, asset_ids)})
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/health", path)
        if match:
            self.send_json({"asset": check_asset_health(self.app.db, match.group(1))})
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/review", path)
        if match:
            payload = self.read_json()
            self.send_json({"asset": review_asset(self.app.db, match.group(1), str(payload.get("action") or ""), str(payload.get("note") or ""))})
            return
        match = re.fullmatch(r"/api/assets/([^/]+)/content-review", path)
        if match:
            payload = self.read_json()
            self.send_json({"asset": review_content_asset(self.app.db, match.group(1), str(payload.get("verdict") or ""), str(payload.get("note") or ""))})
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
            self.send_json({"job": self.app.queue_render(project_id)}, HTTPStatus.ACCEPTED)
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)/retry", path)
        if match:
            self.send_json({"job": self.app.retry_job(match.group(1))}, HTTPStatus.ACCEPTED)
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/assets", path)
        if match:
            project_id = match.group(1)
            payload = self.read_json()
            asset_id = str(payload.get("asset_id") or "")
            self.send_json({"project": add_project_asset(self.app.db, project_id, asset_id, payload)})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/assets/([^/]+)/move", path)
        if match:
            payload = self.read_json()
            self.send_json({"clip": move_project_asset(self.app.db, match.group(1), match.group(2), str(payload.get("direction") or ""))})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/assets/([^/]+)", path)
        if match:
            self.send_json({"clip": update_project_asset(self.app.db, match.group(1), match.group(2), self.read_json())})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/publish", path)
        if match:
            project = self.app.db.get("projects", "id", match.group(1))
            if not project:
                raise KeyError("Project not found")
            payload = self.read_json()
            platforms = list(payload.get("platforms") or ["youtube"])
            jobs = create_publish_jobs(self.app.db, project, platforms, payload.get("scheduled_for"), payload.get("account_id"), payload.get("account_ids"))
            self.send_json({"jobs": [publish_job_view(job) for job in jobs]}, HTTPStatus.CREATED)
            return
        match = re.fullmatch(r"/api/publishing/accounts/([^/]+)/check", path)
        if match:
            self.send_json({"account": check_publishing_account(self.app.db, match.group(1))})
            return
        match = re.fullmatch(r"/api/publishing/jobs/([^/]+)/publish", path)
        if match:
            job_id = match.group(1)
            job = self.app.db.get("publish_jobs", "id", job_id)
            if not job:
                raise KeyError("Publish job not found")
            if not claim_publish_job(self.app.db, job_id):
                raise ValueError("Publish job is already being processed or is not ready")
            claimed = self.app.db.get("publish_jobs", "id", job_id) or job
            attempt: dict[str, Any] | None = None
            try:
                attempt = begin_publish_attempt(self.app.db, job_id, "manual")
                remote_id = publish_job(self.app.db, claimed)
            except Exception as error:
                finalize_publish_failure(self.app.db, job_id, attempt, error)
                self.send_error_json(HTTPStatus.CONFLICT, str(error))
                return
            finalize_publish_success(self.app.db, job_id, attempt, remote_id)
            self.send_json({"job": publish_job_view(self.app.db.get("publish_jobs", "id", job_id) or {})})
            return
        match = re.fullmatch(r"/api/publishing/jobs/([^/]+)/mark-published", path)
        if match:
            job_id = match.group(1)
            job = self.app.db.get("publish_jobs", "id", job_id)
            if not job:
                raise KeyError("Publish job not found")
            payload = self.read_json()
            marked = mark_publish_job(self.app.db, job_id, str(payload.get("remote_id") or "manual"))
            self.send_json({"job": publish_job_view(marked)})
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")

    def handle_api_patch(self, path: str) -> None:
        match = re.fullmatch(r"/api/automations/([^/]+)", path)
        if match:
            self.send_json({"automation": update_automation(self.app.db, match.group(1), self.read_json())})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            self.send_json({"project": update_project(self.app.db, match.group(1), self.read_json())})
            return
        match = re.fullmatch(r"/api/publishing/accounts/([^/]+)", path)
        if match:
            self.send_json({"account": update_publishing_account(self.app.db, match.group(1), self.read_json())})
            return
        match = re.fullmatch(r"/api/publishing/jobs/([^/]+)", path)
        if match:
            payload = self.read_json()
            if "scheduled_for" not in payload:
                raise ValueError("scheduled_for is required")
            self.send_json({"job": reschedule_publish_job(self.app.db, match.group(1), str(payload.get("scheduled_for") or ""))})
            return
        if path == "/api/assets/content-review/bulk":
            payload = self.read_json()
            asset_ids = payload.get("ids")
            if not isinstance(asset_ids, list):
                raise ValueError("ids must be a list")
            self.send_json({"assets": bulk_review_content_assets(self.app.db, asset_ids, str(payload.get("verdict") or ""), str(payload.get("note") or ""))})
            return
        if path == "/api/assets/bulk":
            payload = self.read_json()
            asset_ids = payload.get("ids")
            if not isinstance(asset_ids, list):
                raise ValueError("ids must be a list")
            self.send_json({"assets": bulk_review_assets(self.app.db, asset_ids, str(payload.get("action") or ""), str(payload.get("note") or ""))})
            return
        match = re.fullmatch(r"/api/assets/([^/]+)", path)
        if match:
            asset_id = match.group(1)
            if not self.app.db.get("assets", "id", asset_id):
                raise KeyError("Asset not found")
            payload = self.read_json()
            allowed = {"title", "source_url", "platform", "status", "rights_status"}
            values = {key: payload[key] for key in allowed if key in payload}
            if "title" in values:
                values["title"] = str(values["title"] or "Untitled source").strip()[:160]
            if "source_url" in values:
                values["source_url"] = str(values["source_url"] or "").strip()[:1000]
            if "tags" in payload:
                tags = payload["tags"]
                if isinstance(tags, str):
                    tags = tags.split(",")
                if not isinstance(tags, list):
                    raise ValueError("tags must be a list or comma-separated string")
                values["tags_json"] = json_dumps(sorted({str(tag).strip().lower() for tag in tags if str(tag).strip()}))
            if "status" in values and values["status"] not in {"candidate", "approved", "rejected", "ready"}:
                raise ValueError("Unsupported asset status")
            if "rights_status" in values and values["rights_status"] not in {"private_reference", "owned_distribution", "licensed_distribution", "blocked", "pending_review"}:
                raise ValueError("Unsupported rights status")
            decision = None
            if values.get("rights_status") == "owned_distribution":
                decision = "approve_owned"
            elif values.get("rights_status") == "licensed_distribution":
                decision = "approve_licensed"
            elif values.get("rights_status") == "blocked" or values.get("status") == "rejected":
                decision = "reject"
            if decision:
                descriptive = {key: value for key, value in values.items() if key not in {"status", "rights_status"}}
                if descriptive:
                    descriptive["updated_at"] = now_iso()
                    self.app.db.update("assets", "id", asset_id, descriptive)
                self.send_json({"asset": review_asset(self.app.db, asset_id, decision, str(payload.get("note") or ""))})
                return
            if values:
                values["updated_at"] = now_iso()
                self.app.db.update("assets", "id", asset_id, values)
            self.send_json({"asset": asset_view(self.app.db.get("assets", "id", asset_id) or {})})
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "API route not found")

    def handle_api_delete(self, path: str) -> None:
        match = re.fullmatch(r"/api/automations/([^/]+)", path)
        if match:
            delete_automation(self.app.db, match.group(1))
            self.send_json({"deleted": match.group(1)})
            return
        match = re.fullmatch(r"/api/publishing/accounts/([^/]+)", path)
        if match:
            delete_publishing_account(self.app.db, match.group(1))
            self.send_json({"deleted": match.group(1)})
            return
        match = re.fullmatch(r"/api/publishing/jobs/([^/]+)", path)
        if match:
            self.send_json({"job": cancel_publish_job(self.app.db, match.group(1))})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)/assets/([^/]+)", path)
        if match:
            self.send_json({"project": remove_project_asset(self.app.db, match.group(1), match.group(2))})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            delete_project(self.app.db, match.group(1), self.app.settings.output_dir)
            self.send_json({"deleted": match.group(1)})
            return
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
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "").strip()
        if range_header:
            invalid_range = False
            if not range_header.startswith("bytes=") or "," in range_header:
                invalid_range = True
            else:
                spec = range_header.removeprefix("bytes=").strip()
                if "-" not in spec:
                    invalid_range = True
                else:
                    raw_start, raw_end = (part.strip() for part in spec.split("-", 1))
                    try:
                        if not raw_start:
                            suffix_length = int(raw_end)
                            if suffix_length <= 0 or size <= 0:
                                invalid_range = True
                            else:
                                start = max(size - suffix_length, 0)
                                end = size - 1
                        else:
                            start = int(raw_start)
                            end = int(raw_end) if raw_end else size - 1
                            if start < 0 or start >= size or end < start:
                                invalid_range = True
                            else:
                                end = min(end, size - 1)
                    except ValueError:
                        invalid_range = True
            if invalid_range:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        content_length = max(end - start + 1, 0)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        try:
            with path.open("rb") as source:
                source.seek(start)
                remaining = content_length
                while remaining:
                    chunk = source.read(min(FILE_STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(app_settings: Settings = settings) -> None:
    global STATE
    STATE = AppState(app_settings)
    httpd = ThreadingHTTPServer((app_settings.host, app_settings.port), Handler)
    scheduler = threading.Thread(target=STATE.scheduler_loop, name="dereel-scheduler", daemon=True)
    scheduler.start()
    print(f"DeReel running at http://{app_settings.host}:{app_settings.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.stop_event.set()
        scheduler.join(timeout=2)
        httpd.server_close()
