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
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

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
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '2');
                """
            )
            asset_columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)").fetchall()}
            if "automation_id" not in asset_columns:
                connection.execute("ALTER TABLE assets ADD COLUMN automation_id TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_automation ON assets(automation_id)")
            automation_columns = {row[1] for row in connection.execute("PRAGMA table_info(automations)").fetchall()}
            automation_migrations = {
                "source_mode": "TEXT NOT NULL DEFAULT 'watch'",
                "provider": "TEXT NOT NULL DEFAULT ''",
                "search_query": "TEXT NOT NULL DEFAULT ''",
                "provider_limit": "INTEGER NOT NULL DEFAULT 12",
            }
            for column, definition in automation_migrations.items():
                if column not in automation_columns:
                    connection.execute(f"ALTER TABLE automations ADD COLUMN {column} {definition}")

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
