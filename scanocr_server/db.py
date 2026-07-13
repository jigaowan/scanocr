from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications(
  id TEXT PRIMARY KEY,
  source_title TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  source_language_override TEXT,
  target_language_override TEXT,
  ocr_engine_override TEXT,
  translation_engine_override TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_capture_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures(
  id TEXT PRIMARY KEY,
  application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  client_id TEXT NOT NULL,
  client_name TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  capture_mode TEXT NOT NULL,
  image_path TEXT NOT NULL,
  thumbnail_path TEXT,
  thumbnail_status TEXT NOT NULL,
  thumbnail_width INTEGER,
  thumbnail_height INTEGER,
  thumbnail_error_code TEXT,
  thumbnail_generation INTEGER NOT NULL DEFAULT 1,
  thumbnail_attempts INTEGER NOT NULL DEFAULT 0,
  image_width INTEGER NOT NULL,
  image_height INTEGER NOT NULL,
  status TEXT NOT NULL,
  current_ocr_run_id TEXT,
  current_translation_run_id TEXT,
  ocr_generation INTEGER NOT NULL DEFAULT 0,
  translation_generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS captures_application_time ON captures(application_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS ocr_runs(
  id TEXT PRIMARY KEY,
  capture_id TEXT NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,
  engine_id TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  model_name TEXT,
  requested_language TEXT NOT NULL,
  detected_language TEXT,
  settings_snapshot_json TEXT NOT NULL,
  status TEXT NOT NULL,
  text TEXT,
  confidence REAL,
  regions_json TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS translation_runs(
  id TEXT PRIMARY KEY,
  capture_id TEXT NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  ocr_run_id TEXT NOT NULL REFERENCES ocr_runs(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,
  engine_id TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  model_name TEXT,
  source_language TEXT NOT NULL,
  target_language TEXT NOT NULL,
  settings_snapshot_json TEXT NOT NULL,
  status TEXT NOT NULL,
  text TEXT,
  detected_source_language TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def one(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self.connection.execute(sql, params)


def decode_json_columns(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return row
    for key in ("settings_snapshot_json", "regions_json"):
        if key in row:
            raw = row.pop(key)
            row[key[:-5]] = json.loads(raw) if raw else None
    return row
