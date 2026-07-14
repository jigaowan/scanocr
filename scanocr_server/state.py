from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageCms

from .config import Config
from .db import Database, decode_json_columns
from .engines import EngineError, EngineRegistry

LOG = logging.getLogger("scanocr")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


class Conflict(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NotFound(RuntimeError):
    pass


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[queue.Queue] = []

    def publish(self, change_type: str, **fields: Any) -> None:
        event = {
            "change_type": change_type,
            "occurred_at": now(),
            "application_id": fields.get("application_id"),
            "capture_id": fields.get("capture_id"),
            "run_id": fields.get("run_id"),
            "thumbnail_generation": fields.get("thumbnail_generation"),
            "status": fields.get("status"),
        }
        with self._lock:
            targets = list(self._subscribers)
        for target in targets:
            try:
                target.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        target: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(target)
        return target

    def unsubscribe(self, target: queue.Queue) -> None:
        with self._lock:
            if target in self._subscribers:
                self._subscribers.remove(target)


class ServerState:
    def __init__(self, config: Config):
        self.config = config
        for name in ("images", "thumbnails", "logs", "tmp"):
            (config.data_dir / name).mkdir(parents=True, exist_ok=True)
        self.db = Database(config.data_dir / "scanocr.sqlite3")
        self.registry = EngineRegistry(config.helper)
        self.events = EventBus()
        self.thumbnail_queue: queue.Queue = queue.Queue()
        self.ocr_queue: queue.Queue = queue.Queue()
        self.translation_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.settings = self._load_settings()
        self._restart_config = {
            "host": config.host,
            "port": config.port,
            "open_browser": config.open_browser,
            "max_upload_bytes": config.max_upload_bytes,
            "token": config.token,
        }
        self._recover()
        self._threads = [
            threading.Thread(target=self._thumbnail_worker, name="thumbnail-worker", daemon=True),
            threading.Thread(target=self._ocr_worker, name="ocr-worker", daemon=True),
            threading.Thread(target=self._translation_worker, name="translation-worker", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _load_settings(self) -> Dict[str, Any]:
        defaults = {
            "host": self.config.host,
            "port": self.config.port,
            "open_browser": self.config.open_browser,
            "max_upload_bytes": self.config.max_upload_bytes,
            "token": "********",
            "default_ocr_engine": self.config.ocr_engine,
            "default_translation_engine": self.config.translation_engine,
            "default_source_language": self.config.source_language,
            "default_target_language": self.config.target_language,
            "data_dir": str(self.config.data_dir),
            "database_path": str(self.config.data_dir / "scanocr.sqlite3"),
        }
        stored = self.db.one("SELECT value_json FROM settings WHERE key = 'global'")
        if stored:
            saved = json.loads(stored["value_json"])
            for key in (
                "default_ocr_engine", "default_translation_engine", "default_source_language",
                "default_target_language",
            ):
                if key in saved:
                    defaults[key] = saved[key]
        return defaults

    def update_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        if self.config.managed:
            raise ValueError("global settings are managed by external deployment configuration")
        allowed_live = {
            "default_ocr_engine", "default_translation_engine", "default_source_language",
            "default_target_language",
        }
        allowed_restart = {"host", "port", "open_browser", "max_upload_bytes", "token"}
        unknown = set(changes) - allowed_live - allowed_restart
        if unknown:
            raise ValueError("unknown setting fields: %s" % ", ".join(sorted(unknown)))
        if "default_ocr_engine" in changes:
            self.registry.descriptor(str(changes["default_ocr_engine"]), "ocr")
        if "default_translation_engine" in changes:
            self.registry.descriptor(str(changes["default_translation_engine"]), "translation")
        if "max_upload_bytes" in changes and int(changes["max_upload_bytes"]) <= 0:
            raise ValueError("max_upload_bytes must be positive")
        for key in allowed_live:
            if key in changes:
                self.settings[key] = changes[key]
        for key in allowed_restart:
            if key in changes:
                self._restart_config[key] = changes[key]
                if key != "token":
                    self.settings[key] = changes[key]
        persisted = {key: self.settings[key] for key in allowed_live}
        self.db.execute(
            "INSERT INTO settings(key, value_json) VALUES('global', ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (json.dumps(persisted, ensure_ascii=False),),
        )
        if set(changes) & allowed_restart:
            self._write_config()
        result = self.settings.copy()
        result["restart_required"] = bool(set(changes) & allowed_restart)
        self.events.publish("settings_updated")
        return result

    def _write_config(self) -> None:
        def quoted(value: Any) -> str:
            return json.dumps(str(value), ensure_ascii=False)

        lines = [
            "[server]",
            "host = %s" % quoted(self._restart_config["host"]),
            "port = %d" % int(self._restart_config["port"]),
            "open_browser = %s" % ("true" if self._restart_config["open_browser"] else "false"),
            "managed = false",
            "max_upload_bytes = %d" % int(self._restart_config["max_upload_bytes"]),
            "",
            "[auth]",
            "token = %s" % quoted(self._restart_config["token"]),
            "",
            "[defaults]",
            "ocr_engine = %s" % quoted(self.settings["default_ocr_engine"]),
            "translation_engine = %s" % quoted(self.settings["default_translation_engine"]),
            "source_language = %s" % quoted(self.settings["default_source_language"]),
            "target_language = %s" % quoted(self.settings["default_target_language"]),
            "",
            "[paths]",
            "data_dir = %s" % quoted(self.config.data_dir),
            "helper = %s" % quoted(self.config.helper),
            "",
        ]
        descriptor, temporary_name = tempfile.mkstemp(prefix=".server-", suffix=".toml", dir=str(self.config.path.parent))
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("\n".join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.config.path)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def _recover(self) -> None:
        timestamp = now()
        with self.db.transaction() as connection:
            connection.execute("UPDATE captures SET thumbnail_status='pending', updated_at=? WHERE thumbnail_status='generating'", (timestamp,))
            connection.execute("UPDATE ocr_runs SET status='queued' WHERE status='running'")
            connection.execute("UPDATE translation_runs SET status='queued' WHERE status='running'")
        for item in self.db.all("SELECT id, thumbnail_generation FROM captures WHERE thumbnail_status='pending'"):
            self.thumbnail_queue.put((item["id"], item["thumbnail_generation"]))
        for item in self.db.all("SELECT id FROM ocr_runs WHERE status='queued'"):
            self.ocr_queue.put(item["id"])
        for item in self.db.all("SELECT id FROM translation_runs WHERE status='queued'"):
            self.translation_queue.put(item["id"])

    def resolve_settings(self, application: Dict[str, Any]) -> Dict[str, str]:
        return {
            "source_language": application["source_language_override"] or self.settings["default_source_language"],
            "target_language": application["target_language_override"] or self.settings["default_target_language"],
            "ocr_engine": application["ocr_engine_override"] or self.settings["default_ocr_engine"],
            "translation_engine": application["translation_engine_override"] or self.settings["default_translation_engine"],
        }

    def create_capture(self, metadata: Dict[str, Any], temporary_image: Path, width: int, height: int) -> Tuple[Dict[str, Any], bool]:
        capture_id = metadata["capture_id"]
        existing = self.capture(capture_id)
        if existing:
            temporary_image.unlink(missing_ok=True)
            return existing, False
        title = metadata["application"]["title"]
        timestamp = now()
        application_id = new_id()
        created_application = False
        final_path: Optional[Path] = None
        try:
            with self.db.transaction() as connection:
                row = connection.execute("SELECT * FROM applications WHERE source_title=?", (title,)).fetchone()
                if row:
                    application_id = row["id"]
                    connection.execute(
                        "UPDATE applications SET last_capture_at=?, updated_at=? WHERE id=?",
                        (metadata["captured_at"], timestamp, application_id),
                    )
                else:
                    created_application = True
                    connection.execute(
                        "INSERT INTO applications(id, source_title, display_name, created_at, updated_at, last_capture_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (application_id, title, title, timestamp, timestamp, metadata["captured_at"]),
                    )
                image_dir = self.config.data_dir / "images" / application_id
                image_dir.mkdir(parents=True, exist_ok=True)
                final_path = image_dir / (capture_id + ".png")
                os.replace(str(temporary_image), str(final_path))
                connection.execute(
                    "INSERT INTO captures(id, application_id, client_id, client_name, captured_at, received_at, "
                    "capture_mode, image_path, thumbnail_status, image_width, image_height, status, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (capture_id, application_id, metadata["client_id"], metadata["client_name"],
                     metadata["captured_at"], timestamp, metadata["capture_mode"], str(final_path), "pending",
                     width, height, "queued", timestamp, timestamp),
                )
        except Exception:
            temporary_image.unlink(missing_ok=True)
            if final_path:
                final_path.unlink(missing_ok=True)
            raise
        if created_application:
            self.events.publish("application_created", application_id=application_id)
        self.events.publish("capture_created", application_id=application_id, capture_id=capture_id, status="queued")
        self.events.publish("thumbnail_status_changed", application_id=application_id, capture_id=capture_id,
                            thumbnail_generation=1, status="pending")
        self.thumbnail_queue.put((capture_id, 1))
        self.create_ocr_run(capture_id, {}, translate_after_success=True)
        return self.capture(capture_id), True

    def applications(self) -> List[Dict[str, Any]]:
        rows = self.db.all(
            "SELECT a.*, COUNT(c.id) AS capture_count FROM applications a "
            "LEFT JOIN captures c ON c.application_id=a.id GROUP BY a.id ORDER BY a.last_capture_at DESC"
        )
        for row in rows:
            row["resolved_settings"] = self.resolve_settings(row)
            row["storage_bytes"] = self._application_storage_bytes(row["id"])
            latest = self.db.one("SELECT status FROM captures WHERE application_id=? ORDER BY captured_at DESC LIMIT 1", (row["id"],))
            row["latest_status"] = latest["status"] if latest else None
        return rows

    def application(self, application_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM applications WHERE id=?", (application_id,))
        if row:
            row["resolved_settings"] = self.resolve_settings(row)
            count = self.db.one("SELECT COUNT(*) AS count FROM captures WHERE application_id=?", (application_id,))
            row["capture_count"] = count["count"] if count else 0
            row["storage_bytes"] = self._application_storage_bytes(application_id)
        return row

    def application_captures(self, application_id: str, cursor: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if cursor:
            rows = self.db.all(
                "SELECT * FROM captures WHERE application_id=? AND captured_at<? ORDER BY captured_at DESC LIMIT ?",
                (application_id, cursor, min(limit, 100)),
            )
        else:
            rows = self.db.all(
                "SELECT * FROM captures WHERE application_id=? ORDER BY captured_at DESC LIMIT ?",
                (application_id, min(limit, 100)),
            )
        return [self._capture_detail(row) for row in rows]

    def capture(self, capture_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.one("SELECT * FROM captures WHERE id=?", (capture_id,))
        return self._capture_detail(row) if row else None

    def _capture_detail(self, row: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(row)
        result["storage_bytes"] = self._capture_storage_bytes(row)
        result["ocr"] = decode_json_columns(self.db.one("SELECT * FROM ocr_runs WHERE id=?", (row["current_ocr_run_id"],)))
        result["translation"] = decode_json_columns(self.db.one(
            "SELECT * FROM translation_runs WHERE id=?", (row["current_translation_run_id"],)
        ))
        result["latest_ocr_run"] = decode_json_columns(self.db.one(
            "SELECT * FROM ocr_runs WHERE capture_id=? ORDER BY generation DESC LIMIT 1", (row["id"],)
        ))
        result["latest_translation_run"] = decode_json_columns(self.db.one(
            "SELECT * FROM translation_runs WHERE capture_id=? ORDER BY generation DESC LIMIT 1", (row["id"],)
        ))
        return result

    @staticmethod
    def _capture_storage_bytes(capture: Dict[str, Any]) -> int:
        total = 0
        for value in (capture.get("image_path"), capture.get("thumbnail_path")):
            if value:
                try:
                    total += Path(value).stat().st_size
                except OSError:
                    pass
        return total

    def _application_storage_bytes(self, application_id: str) -> int:
        captures = self.db.all(
            "SELECT image_path, thumbnail_path FROM captures WHERE application_id=?",
            (application_id,),
        )
        return sum(self._capture_storage_bytes(capture) for capture in captures)

    def storage_usage(self) -> Dict[str, Any]:
        total_bytes = 0
        database_bytes = 0
        database_names = {"scanocr.sqlite3", "scanocr.sqlite3-wal", "scanocr.sqlite3-shm"}
        for path in self.config.data_dir.rglob("*"):
            try:
                if path.is_file():
                    size = path.stat().st_size
                    total_bytes += size
                    if path.name in database_names:
                        database_bytes += size
            except OSError:
                continue
        captures = self.db.all("SELECT image_path, thumbnail_path FROM captures")
        capture_bytes = sum(self._capture_storage_bytes(capture) for capture in captures)
        application_count = self.db.one("SELECT COUNT(*) AS count FROM applications")
        return {
            "total_bytes": total_bytes,
            "capture_files_bytes": capture_bytes,
            "database_bytes": database_bytes,
            "other_bytes": max(0, total_bytes - capture_bytes - database_bytes),
            "application_count": application_count["count"] if application_count else 0,
            "capture_count": len(captures),
        }

    def update_application(self, application_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "display_name", "source_language_override", "target_language_override",
            "ocr_engine_override", "translation_engine_override",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("invalid application setting fields")
        if "ocr_engine_override" in changes and changes["ocr_engine_override"] is not None:
            self.registry.descriptor(str(changes["ocr_engine_override"]), "ocr")
        if "translation_engine_override" in changes and changes["translation_engine_override"] is not None:
            self.registry.descriptor(str(changes["translation_engine_override"]), "translation")
        if "display_name" in changes and not isinstance(changes["display_name"], str):
            raise ValueError("display_name must be a string")
        assignments = ", ".join("%s=?" % key for key in changes)
        values = tuple(changes.values()) + (now(), application_id)
        with self.db.transaction() as connection:
            if not connection.execute("SELECT 1 FROM applications WHERE id=?", (application_id,)).fetchone():
                raise NotFound("application not found")
            connection.execute("UPDATE applications SET %s, updated_at=? WHERE id=?" % assignments, values)
        self.events.publish("application_updated", application_id=application_id)
        return self.application(application_id)

    def create_ocr_run(self, capture_id: str, overrides: Dict[str, Any], translate_after_success: bool = True) -> Dict[str, Any]:
        capture = self.db.one("SELECT * FROM captures WHERE id=?", (capture_id,))
        if not capture:
            raise NotFound("capture not found")
        active = self.db.one("SELECT id FROM ocr_runs WHERE capture_id=? AND status IN ('queued','running')", (capture_id,))
        if active:
            raise Conflict("ocr_already_running", "an OCR run is already active")
        application = self.db.one("SELECT * FROM applications WHERE id=?", (capture["application_id"],))
        resolved = self.resolve_settings(application)
        engine_id = str(overrides.get("engine_id", resolved["ocr_engine"]))
        source_language = str(overrides.get("source_language", resolved["source_language"]))
        descriptor = self.registry.descriptor(engine_id, "ocr")
        run_id = new_id()
        generation = int(capture["ocr_generation"]) + 1
        timestamp = now()
        snapshot = {"engine_id": engine_id, "source_language": source_language,
                    "translate_after_success": bool(translate_after_success)}
        interrupted_translations = self.db.all(
            "SELECT id FROM translation_runs WHERE capture_id=? AND status IN ('queued','running')", (capture_id,)
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE translation_runs SET status='interrupted', completed_at=? "
                "WHERE capture_id=? AND status IN ('queued','running')", (timestamp, capture_id),
            )
            connection.execute(
                "INSERT INTO ocr_runs(id,capture_id,generation,engine_id,engine_version,model_name,requested_language,"
                "settings_snapshot_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, capture_id, generation, engine_id, descriptor.implementation_version, descriptor.model_name,
                 source_language, json.dumps(snapshot), "queued", timestamp),
            )
            connection.execute(
                "UPDATE captures SET ocr_generation=?, translation_generation=translation_generation+?, "
                "status='queued', updated_at=? WHERE id=?",
                (generation, 1 if interrupted_translations else 0, timestamp, capture_id),
            )
        for interrupted in interrupted_translations:
            self.events.publish(
                "translation_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                run_id=interrupted["id"], status="interrupted",
            )
        self.events.publish("ocr_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                            run_id=run_id, status="queued")
        self.events.publish("capture_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                            status="queued")
        self.ocr_queue.put(run_id)
        return decode_json_columns(self.db.one("SELECT * FROM ocr_runs WHERE id=?", (run_id,)))

    def create_translation_run(self, capture_id: str, overrides: Dict[str, Any]) -> Dict[str, Any]:
        capture = self.db.one("SELECT * FROM captures WHERE id=?", (capture_id,))
        if not capture:
            raise NotFound("capture not found")
        if not capture["current_ocr_run_id"]:
            raise Conflict("ocr_result_missing", "translation requires a successful current OCR result")
        active = self.db.one(
            "SELECT id FROM translation_runs WHERE capture_id=? AND status IN ('queued','running')", (capture_id,)
        )
        if active:
            raise Conflict("translation_already_running", "a translation run is already active")
        ocr = self.db.one("SELECT * FROM ocr_runs WHERE id=?", (capture["current_ocr_run_id"],))
        application = self.db.one("SELECT * FROM applications WHERE id=?", (capture["application_id"],))
        resolved = self.resolve_settings(application)
        engine_id = str(overrides.get("engine_id", resolved["translation_engine"]))
        source_language = str(overrides.get("source_language", ocr["detected_language"] or resolved["source_language"]))
        target_language = str(overrides.get("target_language", resolved["target_language"]))
        if not target_language:
            raise ValueError("target_language is required")
        descriptor = self.registry.descriptor(engine_id, "translation")
        run_id = new_id()
        generation = int(capture["translation_generation"]) + 1
        timestamp = now()
        snapshot = {"engine_id": engine_id, "source_language": source_language, "target_language": target_language}
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO translation_runs(id,capture_id,ocr_run_id,generation,engine_id,engine_version,model_name,"
                "source_language,target_language,settings_snapshot_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, capture_id, ocr["id"], generation, engine_id, descriptor.implementation_version,
                 descriptor.model_name, source_language, target_language, json.dumps(snapshot), "queued", timestamp),
            )
            connection.execute(
                "UPDATE captures SET translation_generation=?, status='translation_running', updated_at=? WHERE id=?",
                (generation, timestamp, capture_id),
            )
        self.events.publish("translation_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                            run_id=run_id, status="queued")
        self.events.publish("capture_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                            status="translation_running")
        self.translation_queue.put(run_id)
        return decode_json_columns(self.db.one("SELECT * FROM translation_runs WHERE id=?", (run_id,)))

    def retry_thumbnail(self, capture_id: str) -> Dict[str, Any]:
        capture = self.db.one("SELECT * FROM captures WHERE id=?", (capture_id,))
        if not capture:
            raise NotFound("capture not found")
        if capture["thumbnail_status"] in ("pending", "generating"):
            raise Conflict("thumbnail_already_running", "thumbnail generation is already active")
        if capture["thumbnail_status"] == "ready":
            raise Conflict("thumbnail_already_ready", "thumbnail is already ready")
        generation = int(capture["thumbnail_generation"]) + 1
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE captures SET thumbnail_status='pending', thumbnail_generation=?, thumbnail_error_code=NULL, "
                "thumbnail_path=NULL, updated_at=? WHERE id=?", (generation, now(), capture_id),
            )
        self.thumbnail_queue.put((capture_id, generation))
        self.events.publish("thumbnail_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                            thumbnail_generation=generation, status="pending")
        return {"capture_id": capture_id, "thumbnail_generation": generation, "thumbnail_status": "pending"}

    def delete_capture(self, capture_id: str) -> None:
        capture = self.db.one("SELECT * FROM captures WHERE id=?", (capture_id,))
        if not capture:
            raise NotFound("capture not found")
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM captures WHERE id=?", (capture_id,))
        self._remove_capture_files([capture])
        self.events.publish("capture_deleted", application_id=capture["application_id"], capture_id=capture_id)

    @staticmethod
    def _remove_capture_files(captures: List[Dict[str, Any]]) -> None:
        for capture in captures:
            for path in (capture.get("image_path"), capture.get("thumbnail_path")):
                if not path:
                    continue
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    LOG.exception("failed to remove capture file for %s", capture.get("id", "unknown"))

    def delete_application(self, application_id: str) -> None:
        application = self.db.one("SELECT id FROM applications WHERE id=?", (application_id,))
        if not application:
            raise NotFound("application not found")
        captures = self.db.all(
            "SELECT id, image_path, thumbnail_path FROM captures WHERE application_id=?",
            (application_id,),
        )
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM applications WHERE id=?", (application_id,))
        self._remove_capture_files(captures)
        for name in ("images", "thumbnails"):
            try:
                shutil.rmtree(self.config.data_dir / name / application_id, ignore_errors=True)
            except OSError:
                LOG.exception("failed to remove application directory for %s", application_id)
        self.events.publish("application_deleted", application_id=application_id)

    def clear_data(self) -> Dict[str, Any]:
        previous = self.storage_usage()
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM applications")
        for name in ("images", "thumbnails", "logs", "tmp"):
            directory = self.config.data_dir / name
            try:
                shutil.rmtree(directory, ignore_errors=True)
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                LOG.exception("failed to clear data directory %s", directory)
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            LOG.exception("failed to compact database after clearing data")
        self.events.publish("data_cleared")
        return {"cleared": previous, "storage": self.storage_usage()}

    def _thumbnail_worker(self) -> None:
        while not self.stop_event.is_set():
            capture_id, generation = self.thumbnail_queue.get()
            capture = self.db.one("SELECT * FROM captures WHERE id=?", (capture_id,))
            if not capture or capture["thumbnail_generation"] != generation or capture["thumbnail_status"] != "pending":
                continue
            timestamp = now()
            self.db.execute(
                "UPDATE captures SET thumbnail_status='generating', thumbnail_attempts=thumbnail_attempts+1, updated_at=? "
                "WHERE id=? AND thumbnail_generation=?", (timestamp, capture_id, generation),
            )
            self.events.publish("thumbnail_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                                thumbnail_generation=generation, status="generating")
            directory = self.config.data_dir / "thumbnails" / capture["application_id"]
            directory.mkdir(parents=True, exist_ok=True)
            final = directory / (capture_id + ".webp")
            temporary = directory / (".%s.%d.tmp.webp" % (capture_id, generation))
            try:
                with Image.open(capture["image_path"]) as source:
                    image = source.convert("RGBA") if "A" in source.getbands() else source.convert("RGB")
                    image.thumbnail((480, 320), Image.Resampling.LANCZOS)
                    image.save(str(temporary), "WEBP", quality=82, method=6, lossless=False, exact=True)
                    thumb_width, thumb_height = image.size
                current = self.db.one("SELECT thumbnail_generation, thumbnail_status FROM captures WHERE id=?", (capture_id,))
                if not current or current["thumbnail_generation"] != generation or current["thumbnail_status"] != "generating":
                    temporary.unlink(missing_ok=True)
                    continue
                os.replace(str(temporary), str(final))
                self.db.execute(
                    "UPDATE captures SET thumbnail_status='ready', thumbnail_path=?, thumbnail_width=?, thumbnail_height=?, "
                    "thumbnail_error_code=NULL, updated_at=? WHERE id=? AND thumbnail_generation=?",
                    (str(final), thumb_width, thumb_height, now(), capture_id, generation),
                )
                self.events.publish("thumbnail_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                                    thumbnail_generation=generation, status="ready")
            except Exception as error:
                temporary.unlink(missing_ok=True)
                LOG.exception("thumbnail generation failed for %s", capture_id)
                self.db.execute(
                    "UPDATE captures SET thumbnail_status='failed', thumbnail_error_code='execution_failed', updated_at=? "
                    "WHERE id=? AND thumbnail_generation=?", (now(), capture_id, generation),
                )
                self.events.publish("thumbnail_status_changed", application_id=capture["application_id"], capture_id=capture_id,
                                    thumbnail_generation=generation, status="failed")

    def _ocr_worker(self) -> None:
        while not self.stop_event.is_set():
            run_id = self.ocr_queue.get()
            run = self.db.one("SELECT * FROM ocr_runs WHERE id=?", (run_id,))
            if not run or run["status"] != "queued":
                continue
            capture = self.db.one("SELECT * FROM captures WHERE id=?", (run["capture_id"],))
            if not capture or capture["ocr_generation"] != run["generation"]:
                continue
            self.db.execute("UPDATE ocr_runs SET status='running' WHERE id=?", (run_id,))
            self.db.execute("UPDATE captures SET status='ocr_running', updated_at=? WHERE id=?", (now(), capture["id"]))
            self.events.publish("ocr_status_changed", application_id=capture["application_id"], capture_id=capture["id"],
                                run_id=run_id, status="running")
            self.events.publish("capture_status_changed", application_id=capture["application_id"], capture_id=capture["id"],
                                status="ocr_running")
            try:
                result = self.registry.recognize(run["engine_id"], Path(capture["image_path"]), run["requested_language"])
                with self.db.transaction() as connection:
                    current = connection.execute("SELECT ocr_generation FROM captures WHERE id=?", (capture["id"],)).fetchone()
                    if not current or current["ocr_generation"] != run["generation"]:
                        continue
                    connection.execute(
                        "UPDATE ocr_runs SET status='succeeded', text=?, detected_language=?, confidence=?, regions_json=?, "
                        "model_name=?, completed_at=? WHERE id=?",
                        (result.get("text", ""), result.get("detected_language"), result.get("confidence"),
                         json.dumps(result.get("regions"), ensure_ascii=False), result.get("model_name"), now(), run_id),
                    )
                    connection.execute(
                        "UPDATE captures SET current_ocr_run_id=?, current_translation_run_id=NULL, status='ready', updated_at=? "
                        "WHERE id=?", (run_id, now(), capture["id"]),
                    )
                self.events.publish("ocr_status_changed", application_id=capture["application_id"], capture_id=capture["id"],
                                    run_id=run_id, status="succeeded")
                snapshot = json.loads(run["settings_snapshot_json"])
                if snapshot.get("translate_after_success"):
                    app = self.db.one("SELECT * FROM applications WHERE id=?", (capture["application_id"],))
                    if self.resolve_settings(app)["target_language"]:
                        self.create_translation_run(capture["id"], {})
                    else:
                        self.events.publish("capture_status_changed", application_id=capture["application_id"],
                                            capture_id=capture["id"], status="ready")
            except (EngineError, Exception) as error:
                code = error.code if isinstance(error, EngineError) else "execution_failed"
                LOG.warning("OCR run %s failed: %s", run_id, error)
                with self.db.transaction() as connection:
                    connection.execute(
                        "UPDATE ocr_runs SET status='failed', error_code=?, error_message=?, completed_at=? WHERE id=?",
                        (code, str(error)[:1000], now(), run_id),
                    )
                    connection.execute(
                        "UPDATE captures SET status='ocr_failed', updated_at=? WHERE id=? AND ocr_generation=?",
                        (now(), capture["id"], run["generation"]),
                    )
                self.events.publish("ocr_status_changed", application_id=capture["application_id"], capture_id=capture["id"],
                                    run_id=run_id, status="failed")
                self.events.publish("capture_status_changed", application_id=capture["application_id"], capture_id=capture["id"],
                                    status="ocr_failed")

    def _translation_worker(self) -> None:
        while not self.stop_event.is_set():
            run_id = self.translation_queue.get()
            run = self.db.one("SELECT * FROM translation_runs WHERE id=?", (run_id,))
            if not run or run["status"] != "queued":
                continue
            capture = self.db.one("SELECT * FROM captures WHERE id=?", (run["capture_id"],))
            ocr = self.db.one("SELECT * FROM ocr_runs WHERE id=?", (run["ocr_run_id"],))
            if not capture or not ocr or capture["translation_generation"] != run["generation"]:
                continue
            self.db.execute("UPDATE translation_runs SET status='running' WHERE id=?", (run_id,))
            self.events.publish("translation_status_changed", application_id=capture["application_id"], capture_id=capture["id"],
                                run_id=run_id, status="running")
            try:
                result = self.registry.translate(
                    run["engine_id"], ocr["text"] or "", run["source_language"], run["target_language"]
                )
                with self.db.transaction() as connection:
                    current = connection.execute(
                        "SELECT translation_generation,current_ocr_run_id FROM captures WHERE id=?", (capture["id"],)
                    ).fetchone()
                    if not current or current["translation_generation"] != run["generation"] or current["current_ocr_run_id"] != run["ocr_run_id"]:
                        connection.execute(
                            "UPDATE translation_runs SET status='interrupted', completed_at=? "
                            "WHERE id=? AND status='running'", (now(), run_id),
                        )
                        continue
                    connection.execute(
                        "UPDATE translation_runs SET status='succeeded', text=?, detected_source_language=?, model_name=?, "
                        "completed_at=? WHERE id=?",
                        (result.get("text", ""), result.get("detected_source_language"), result.get("model_name"), now(), run_id),
                    )
                    connection.execute(
                        "UPDATE captures SET current_translation_run_id=?, status='ready', updated_at=? WHERE id=?",
                        (run_id, now(), capture["id"]),
                    )
                self.events.publish("translation_status_changed", application_id=capture["application_id"],
                                    capture_id=capture["id"], run_id=run_id, status="succeeded")
                self.events.publish("capture_status_changed", application_id=capture["application_id"],
                                    capture_id=capture["id"], status="ready")
            except (EngineError, Exception) as error:
                code = error.code if isinstance(error, EngineError) else "execution_failed"
                LOG.warning("translation run %s failed: %s", run_id, error)
                with self.db.transaction() as connection:
                    connection.execute(
                        "UPDATE translation_runs SET status='failed', error_code=?, error_message=?, completed_at=? WHERE id=?",
                        (code, str(error)[:1000], now(), run_id),
                    )
                    connection.execute(
                        "UPDATE captures SET status='translation_failed', updated_at=? WHERE id=? AND translation_generation=?",
                        (now(), capture["id"], run["generation"]),
                    )
                self.events.publish("translation_status_changed", application_id=capture["application_id"],
                                    capture_id=capture["id"], run_id=run_id, status="failed")
                self.events.publish("capture_status_changed", application_id=capture["application_id"],
                                    capture_id=capture["id"], status="translation_failed")
