from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import queue
import re
import tempfile
import urllib.parse
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .multipart import (
    ChunkedInput,
    LimitedInput,
    MultipartError,
    MultipartStream,
    PayloadTooLarge,
    UnsupportedMedia,
    boundary_from_content_type,
)
from .png import PNG_SIGNATURE, PngError, validate_png
from .state import Conflict, NotFound, ServerState

LOG = logging.getLogger("scanocr")


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def validate_metadata(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(422, "invalid_metadata", "metadata must be a JSON object")
    required = {
        "schema_version", "capture_id", "client_id", "client_name", "captured_at", "capture_mode",
        "platform", "compositor", "image", "application",
    }
    missing = required - set(value)
    if missing:
        raise ApiError(422, "invalid_metadata", "missing metadata fields: %s" % ", ".join(sorted(missing)))
    if value["schema_version"] != 1:
        raise ApiError(422, "unsupported_schema", "schema_version must be 1")
    try:
        uuid.UUID(str(value["capture_id"]))
    except (ValueError, AttributeError):
        raise ApiError(422, "invalid_metadata", "capture_id must be a UUID")
    for key in ("client_id", "client_name", "captured_at", "platform", "compositor"):
        if not isinstance(value[key], str) or (key != "client_name" and not value[key]):
            raise ApiError(422, "invalid_metadata", "%s must be a string" % key)
    try:
        datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(422, "invalid_metadata", "captured_at must be an ISO 8601 timestamp")
    if value["capture_mode"] not in ("active_window", "frozen_region"):
        raise ApiError(422, "invalid_metadata", "invalid capture_mode")
    image = value["image"]
    if not isinstance(image, dict) or image.get("format") != "png":
        raise ApiError(422, "invalid_metadata", "image.format must be png")
    if not isinstance(image.get("width"), int) or not isinstance(image.get("height"), int):
        raise ApiError(422, "invalid_metadata", "image width and height must be integers")
    application = value["application"]
    if not isinstance(application, dict) or "title" not in application:
        raise ApiError(422, "invalid_metadata", "application.title is required")
    if set(application) != {"title"}:
        raise ApiError(422, "invalid_metadata", "application may only contain title")
    if not isinstance(application["title"], str):
        raise ApiError(422, "invalid_metadata", "application.title must be a string")
    return value


class ScanOCRHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ScanOCR/0.2"
    state: ServerState

    def log_message(self, message: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), message % args)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = "Bearer " + self.state.config.token
        return hmac.compare_digest(header.encode("utf-8"), expected.encode("utf-8"))

    def _require_auth(self) -> None:
        if not self._authorized():
            # The request body is intentionally not consumed when auth fails.
            self.close_connection = True
            raise ApiError(401, "unauthorized", "a valid Bearer token is required")

    def _json(self, status: int, value: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="ScanOCR"')
        for key, content in (extra_headers or {}).items():
            self.send_header(key, content)
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self, maximum: int = 1048576) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise ApiError(400, "invalid_request", "invalid Content-Length")
        if length < 0:
            raise ApiError(411, "length_required", "Content-Length is required")
        if length > maximum:
            raise ApiError(413, "payload_too_large", "JSON request is too large")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "invalid_json", "request body must be valid JSON")
        if not isinstance(value, dict):
            raise ApiError(422, "invalid_json", "request body must be a JSON object")
        return value

    def _send_file(self, path: Path, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
        try:
            size = path.stat().st_size
            stream = path.open("rb")
        except OSError:
            raise ApiError(404, "file_not_found", "capture file is unavailable")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        with stream:
            while True:
                block = stream.read(65536)
                if not block:
                    break
                self.wfile.write(block)

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path.startswith("/api/v1/") or path == "/api/v1":
            self._require_auth()
        if method == "GET" and path == "/api/v1/applications":
            self._json(200, {"applications": self.state.applications()})
            return
        match = re.fullmatch(r"/api/v1/applications/([^/]+)", path)
        if method == "GET" and match:
            application = self.state.application(match.group(1))
            if not application:
                raise NotFound("application not found")
            self._json(200, application)
            return
        if method == "DELETE" and match:
            self.state.delete_application(match.group(1))
            self._empty(204)
            return
        match = re.fullmatch(r"/api/v1/applications/([^/]+)/captures", path)
        if method == "GET" and match:
            query = urllib.parse.parse_qs(parsed.query)
            captures = self.state.application_captures(match.group(1), query.get("cursor", [None])[0])
            self._json(200, {"captures": captures, "next_cursor": captures[-1]["captured_at"] if captures else None})
            return
        match = re.fullmatch(r"/api/v1/applications/([^/]+)/settings", path)
        if method == "PUT" and match:
            self._json(200, self.state.update_application(match.group(1), self._read_json()))
            return
        if method == "GET" and path == "/api/v1/engines":
            self._json(200, {"engines": [item.as_dict() for item in self.state.registry.engines.values()]})
            return
        if method == "GET" and path == "/api/v1/settings":
            self._json(200, self.state.settings)
            return
        if method == "PUT" and path == "/api/v1/settings":
            self._json(200, self.state.update_settings(self._read_json()))
            return
        if method == "GET" and path == "/api/v1/storage":
            self._json(200, self.state.storage_usage())
            return
        if method == "DELETE" and path == "/api/v1/storage":
            self._json(200, self.state.clear_data())
            return
        if method == "GET" and path == "/api/v1/events":
            self._events()
            return
        if method == "POST" and path == "/api/v1/captures":
            self._upload()
            return
        match = re.fullmatch(r"/api/v1/captures/([^/]+)$", path)
        if match and method == "GET":
            capture = self.state.capture(match.group(1))
            if not capture:
                raise NotFound("capture not found")
            self._json(200, capture)
            return
        if match and method == "DELETE":
            self.state.delete_capture(match.group(1))
            self._empty(204)
            return
        match = re.fullmatch(r"/api/v1/captures/([^/]+)/image", path)
        if match and method == "GET":
            capture = self.state.capture(match.group(1))
            if not capture:
                raise NotFound("capture not found")
            self._send_file(Path(capture["image_path"]), "image/png", {"Cache-Control": "private, no-cache"})
            return
        match = re.fullmatch(r"/api/v1/captures/([^/]+)/thumbnail", path)
        if match and method == "GET":
            capture = self.state.capture(match.group(1))
            if not capture:
                raise NotFound("capture not found")
            status = capture["thumbnail_status"]
            if status in ("pending", "generating"):
                self._json(202, {"thumbnail_status": status}, {"Retry-After": "1"})
            elif status == "failed":
                raise ApiError(409, "thumbnail_failed", "thumbnail generation failed")
            else:
                etag = '"%s-%s"' % (capture["id"], capture["thumbnail_generation"])
                self._send_file(Path(capture["thumbnail_path"]), "image/webp", {
                    "ETag": etag,
                    "Cache-Control": "private, max-age=31536000, immutable",
                })
            return
        match = re.fullmatch(r"/api/v1/captures/([^/]+)/ocr-runs", path)
        if match and method == "POST":
            body = self._read_json()
            translate_after = bool(body.pop("translate_after_success", True))
            self._json(202, self.state.create_ocr_run(match.group(1), body, translate_after))
            return
        match = re.fullmatch(r"/api/v1/captures/([^/]+)/translation-runs", path)
        if match and method == "POST":
            self._json(202, self.state.create_translation_run(match.group(1), self._read_json()))
            return
        match = re.fullmatch(r"/api/v1/captures/([^/]+)/thumbnail-runs", path)
        if match and method == "POST":
            self._json(202, self.state.retry_thumbnail(match.group(1)))
            return
        if method == "GET" and path in ("/", "/index.html"):
            index = Path(__file__).resolve().parent / "web" / "index.html"
            self._send_file(index, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
            return
        raise ApiError(404, "not_found", "route not found")

    def _upload(self) -> None:
        boundary = boundary_from_content_type(self.headers.get("Content-Type", ""))
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            source = ChunkedInput(self.rfile, self.state.config.max_upload_bytes)
        else:
            try:
                content_length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise ApiError(400, "invalid_request", "invalid Content-Length")
            source = LimitedInput(self.rfile, content_length, self.state.config.max_upload_bytes)
        parser = MultipartStream(source, boundary)
        metadata_data: Optional[bytes] = None
        image_path: Optional[Path] = None
        saw_image = False
        try:
            if not parser.start():
                raise MultipartError("empty multipart body")
            final = False
            while not final:
                part, _ = parser.headers()
                if part.name == "metadata":
                    if metadata_data is not None:
                        raise MultipartError("metadata part is duplicated")
                    if part.content_type != "application/json":
                        raise UnsupportedMedia("metadata part must be application/json")
                    buffer = bytearray()

                    def metadata_consumer(block: bytes) -> None:
                        if len(buffer) + len(block) > 1048576:
                            raise MultipartError("metadata part is too large")
                        buffer.extend(block)

                    final = parser.body(metadata_consumer)
                    metadata_data = bytes(buffer)
                elif part.name == "image":
                    if saw_image:
                        raise MultipartError("image part is duplicated")
                    saw_image = True
                    if part.content_type != "image/png":
                        raise UnsupportedMedia("image part must be image/png")
                    fd, temporary = tempfile.mkstemp(prefix="upload-", suffix=".png", dir=str(self.state.config.data_dir / "tmp"))
                    image_path = Path(temporary)
                    prefix = bytearray()
                    validated = [False]
                    with os.fdopen(fd, "wb") as output:
                        def image_consumer(block: bytes) -> None:
                            if not validated[0]:
                                needed = 8 - len(prefix)
                                prefix.extend(block[:needed])
                                block = block[needed:]
                                if len(prefix) == 8:
                                    if bytes(prefix) != PNG_SIGNATURE:
                                        raise UnsupportedMedia("image does not have a PNG signature")
                                    output.write(prefix)
                                    validated[0] = True
                            if validated[0] and block:
                                output.write(block)

                        final = parser.body(image_consumer)
                    if not validated[0]:
                        raise UnsupportedMedia("image does not have a complete PNG signature")
                else:
                    raise MultipartError("unexpected multipart part: %s" % part.name)
            if parser.buffer:
                raise MultipartError("data follows the final multipart boundary")
            while True:
                trailing = source.read(65536)
                if not trailing:
                    break
                raise MultipartError("data follows the final multipart boundary")
            if isinstance(source, LimitedInput) and source.remaining:
                raise MultipartError("request body is shorter than Content-Length")
            if metadata_data is None or image_path is None:
                raise MultipartError("metadata and image parts are both required")
            try:
                metadata = validate_metadata(json.loads(metadata_data.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(422, "invalid_metadata", "metadata part must contain valid JSON")
            try:
                width, height = validate_png(image_path)
            except PngError as error:
                raise ApiError(422, "invalid_png", str(error))
            capture, created = self.state.create_capture(metadata, image_path, width, height)
            image_path = None
            self._json(202, {
                "capture_id": capture["id"],
                "application_id": capture["application_id"],
                "status": "queued" if created else capture["status"],
                "thumbnail_status": "pending" if created else capture["thumbnail_status"],
                "idempotent_replay": not created,
            })
        finally:
            if image_path:
                image_path.unlink(missing_ok=True)

    def _events(self) -> None:
        subscriber = self.state.events.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = subscriber.get(timeout=15)
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    self.wfile.write(b"event: change\ndata: " + data + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.state.events.unsubscribe(subscriber)

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except ApiError as error:
            self._json(error.status, {"error": {"code": error.code, "message": str(error)}})
        except PayloadTooLarge as error:
            self._json(413, {"error": {"code": "payload_too_large", "message": str(error)}})
        except UnsupportedMedia as error:
            self._json(415, {"error": {"code": "unsupported_media_type", "message": str(error)}})
        except MultipartError as error:
            self._json(400, {"error": {"code": "invalid_multipart", "message": str(error)}})
        except Conflict as error:
            self._json(409, {"error": {"code": error.code, "message": str(error)}})
        except NotFound as error:
            self._json(404, {"error": {"code": "not_found", "message": str(error)}})
        except ValueError as error:
            self._json(422, {"error": {"code": "invalid_input", "message": str(error)}})
        except Exception:
            LOG.exception("unhandled request failure")
            self._json(500, {"error": {"code": "internal_error", "message": "internal server error"}})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


def create_server(state: ServerState) -> ThreadingHTTPServer:
    class BoundHandler(ScanOCRHandler):
        pass

    BoundHandler.state = state
    return ThreadingHTTPServer((state.config.host, state.config.port), BoundHandler)
