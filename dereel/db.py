from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('media', 'reference')),
                    title TEXT NOT NULL,
                    source_url TEXT,
                    platform TEXT,
                    local_path TEXT,
                    mime_type TEXT,
                    duration REAL,
                    width INTEGER,
                    height INTEGER,
                    has_audio INTEGER NOT NULL DEFAULT 0,
                    file_size INTEGER,
                    fingerprint TEXT,
                    automation_id TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    rights_status TEXT NOT NULL CHECK(rights_status IN ('private_reference', 'owned_distribution', 'licensed_distribution', 'blocked', 'pending_review')),
                    status TEXT NOT NULL CHECK(status IN ('candidate', 'approved', 'rejected', 'ready')),
                    analysis_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
                CREATE INDEX IF NOT EXISTS idx_assets_rights ON assets(rights_status);

                CREATE TABLE IF NOT EXISTS asset_reviews (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
                    action TEXT NOT NULL CHECK(action IN ('approve_owned', 'approve_licensed', 'reject')),
                    previous_rights_status TEXT NOT NULL,
                    next_rights_status TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    next_status TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_asset_reviews_asset ON asset_reviews(asset_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    theme TEXT NOT NULL,
                    format TEXT NOT NULL DEFAULT 'vertical_9_16',
                    duration_target REAL NOT NULL DEFAULT 35,
                    style TEXT NOT NULL DEFAULT 'kinetic',
                    status TEXT NOT NULL CHECK(status IN ('draft', 'planning', 'rendering', 'ready', 'failed')),
                    title TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    output_dir TEXT,
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_assets (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
                    order_index INTEGER NOT NULL,
                    in_point REAL NOT NULL DEFAULT 0,
                    out_point REAL,
                    note TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    selected_by TEXT NOT NULL DEFAULT 'manual',
                    PRIMARY KEY(project_id, asset_id)
                );

                CREATE TABLE IF NOT EXISTS automations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    watch_dir TEXT NOT NULL,
                    source_mode TEXT NOT NULL DEFAULT 'watch',
                    provider TEXT NOT NULL DEFAULT '',
                    search_query TEXT NOT NULL DEFAULT '',
                    provider_limit INTEGER NOT NULL DEFAULT 12,
                    theme TEXT NOT NULL,
                    duration_target REAL NOT NULL DEFAULT 35,
                    style TEXT NOT NULL DEFAULT 'kinetic',
                    title_template TEXT NOT NULL DEFAULT '',
                    caption_template TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    default_rights TEXT NOT NULL CHECK(default_rights IN ('pending_review', 'owned_distribution', 'licensed_distribution')),
                    music_asset_id TEXT,
                    music_volume REAL NOT NULL DEFAULT 0.16,
                    sfx_asset_id TEXT,
                    sfx_volume REAL NOT NULL DEFAULT 0.55,
                    sfx_start_time REAL NOT NULL DEFAULT 0,
                    voiceover_asset_id TEXT,
                    voiceover_volume REAL NOT NULL DEFAULT 1.0,
                    voiceover_start_time REAL NOT NULL DEFAULT 0,
                    platforms_json TEXT NOT NULL DEFAULT '[]',
                    interval_minutes INTEGER NOT NULL DEFAULT 0,
                    publish_delay_minutes INTEGER NOT NULL DEFAULT 0,
                    auto_render INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('idle', 'queued', 'running', 'failed')) DEFAULT 'idle',
                    last_run_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_project_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_automations_enabled ON automations(enabled, interval_minutes, status);

                CREATE TABLE IF NOT EXISTS project_audio (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
                    role TEXT NOT NULL CHECK(role IN ('music', 'sfx', 'voiceover')),
                    volume REAL NOT NULL DEFAULT 0.16,
                    start_time REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(project_id, asset_id, role)
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                    automation_id TEXT REFERENCES automations(id) ON DELETE SET NULL,
                    retry_of TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automation_runs (
                    id TEXT PRIMARY KEY,
                    automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
                    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
                    status TEXT NOT NULL CHECK(status IN ('running', 'idle', 'no_sources', 'awaiting_review', 'planned', 'rendered', 'failed')),
                    scan_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_automation_runs_automation ON automation_runs(automation_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_automation_runs_job ON automation_runs(job_id);

                CREATE TABLE IF NOT EXISTS publishing_accounts (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL CHECK(platform IN ('youtube', 'instagram', 'tiktok')),
                    label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'not_connected',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publish_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    account_id TEXT REFERENCES publishing_accounts(id) ON DELETE SET NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'ready', 'published', 'failed')),
                    scheduled_for TEXT,
                    remote_id TEXT,
                    error TEXT,
                    claim_token TEXT,
                    claimed_at TEXT,
                    cancelled_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publish_attempts (
                    id TEXT PRIMARY KEY,
                    publish_job_id TEXT NOT NULL REFERENCES publish_jobs(id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    trigger TEXT NOT NULL CHECK(trigger IN ('manual', 'scheduler')),
                    status TEXT NOT NULL CHECK(status IN ('running', 'published', 'failed')),
                    remote_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(publish_job_id, attempt_number)
                );

                CREATE INDEX IF NOT EXISTS idx_publish_attempts_job ON publish_attempts(publish_job_id, started_at DESC);

                INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '10');
                """
            )
            asset_columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)").fetchall()}
            if "automation_id" not in asset_columns:
                connection.execute("ALTER TABLE assets ADD COLUMN automation_id TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_automation ON assets(automation_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_asset_reviews_asset ON asset_reviews(asset_id, created_at DESC)")
            automation_columns = {row[1] for row in connection.execute("PRAGMA table_info(automations)").fetchall()}
            automation_migrations = {
                "source_mode": "TEXT NOT NULL DEFAULT 'watch'",
                "provider": "TEXT NOT NULL DEFAULT ''",
                "search_query": "TEXT NOT NULL DEFAULT ''",
                "provider_limit": "INTEGER NOT NULL DEFAULT 12",
                "music_volume": "REAL NOT NULL DEFAULT 0.16",
                "sfx_asset_id": "TEXT",
                "sfx_volume": "REAL NOT NULL DEFAULT 0.55",
                "sfx_start_time": "REAL NOT NULL DEFAULT 0",
                "voiceover_asset_id": "TEXT",
                "voiceover_volume": "REAL NOT NULL DEFAULT 1.0",
                "voiceover_start_time": "REAL NOT NULL DEFAULT 0",
            }
            for column, definition in automation_migrations.items():
                if column not in automation_columns:
                    connection.execute(f"ALTER TABLE automations ADD COLUMN {column} {definition}")
            job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
            if "automation_id" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN automation_id TEXT")
            if "retry_of" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN retry_of TEXT")
            if "attempt" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_automation ON jobs(automation_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_retry_of ON jobs(retry_of)")
            publish_columns = {row[1] for row in connection.execute("PRAGMA table_info(publish_jobs)").fetchall()}
            if "claim_token" not in publish_columns:
                connection.execute("ALTER TABLE publish_jobs ADD COLUMN claim_token TEXT")
            if "claimed_at" not in publish_columns:
                connection.execute("ALTER TABLE publish_jobs ADD COLUMN claimed_at TEXT")
            if "cancelled_at" not in publish_columns:
                connection.execute("ALTER TABLE publish_jobs ADD COLUMN cancelled_at TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_publish_claim ON publish_jobs(claim_token)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_publish_attempts_job ON publish_attempts(publish_job_id, started_at DESC)")
            connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '10')")

    @staticmethod
    def row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def insert(self, table: str, values: dict[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connection() as connection:
            connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                list(values.values()),
            )

    def update(self, table: str, key_column: str, key: str, values: dict[str, Any]) -> None:
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self.connection() as connection:
            connection.execute(
                f"UPDATE {table} SET {assignments} WHERE {key_column} = ?",
                [*values.values(), key],
            )

    def get(self, table: str, key_column: str, key: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self.row(connection.execute(f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)).fetchone())

    def delete(self, table: str, key_column: str, key: str) -> None:
        with self.connection() as connection:
            connection.execute(f"DELETE FROM {table} WHERE {key_column} = ?", (key,))
