#!/usr/bin/env python3
from __future__ import annotations

import json
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from scanocr_server.config import Config, ConfigError

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
HELPER = Path(os.environ.get("SCANOCR_TEST_HELPER", ROOT / ".build" / "scanocr-native-helper"))
SERVER = os.environ.get("SCANOCR_TEST_SERVER")
ALLOW_MISSING_TRANSLATION = os.environ.get("SCANOCR_TEST_ALLOW_MISSING_TRANSLATION") == "1"
TOKEN = "correct-token-2026"


def make_test_images(directory: Path):
    font_paths = (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    )
    font_path = next((path for path in font_paths if Path(path).is_file()), None)
    if font_path is None:
        raise AssertionError("a system font with Japanese glyphs is required")

    horizontal = directory / "horizontal.png"
    horizontal_image = Image.new("RGB", (1392, 320), "white")
    horizontal_draw = ImageDraw.Draw(horizontal_image)
    horizontal_draw.multiline_text(
        (70, 55),
        "もう心配する必要はありません\nカリンの姿を捉えています",
        font=ImageFont.truetype(font_path, 64),
        fill="black",
        spacing=28,
    )
    horizontal_image.save(horizontal, "PNG")

    portrait = directory / "portrait.png"
    portrait_image = Image.new("RGB", (800, 1200), "white")
    portrait_draw = ImageDraw.Draw(portrait_image)
    portrait_draw.multiline_text(
        (60, 80),
        "画像認識テスト\n\nこれは二枚目の画像です\n\n文字を正しく読み取ります",
        font=ImageFont.truetype(font_path, 52),
        fill="black",
        spacing=24,
    )
    portrait_image.save(portrait, "PNG")
    return horizontal, portrait


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(base: str, path: str, token: str = TOKEN, method: str = "GET", body=None, headers=None):
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = "Bearer " + token
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = response.read()
            content_type = response.headers.get_content_type()
            return response.status, json.loads(data) if content_type == "application/json" and data else data, response.headers
    except urllib.error.HTTPError as error:
        data = error.read()
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = data
        return error.code, parsed, error.headers
    except urllib.error.URLError as error:
        return 0, {"error": str(error)}, {}


def multipart(metadata, image_path: Path):
    boundary = "scanocr-test-" + uuid.uuid4().hex
    image = image_path.read_bytes()
    metadata_data = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    body = bytearray()
    for name, filename, content_type, value in (
        ("metadata", "metadata.json", "application/json", metadata_data),
        ("image", image_path.name, "image/png", image),
    ):
        body.extend(("--%s\r\n" % boundary).encode())
        body.extend(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename)).encode())
        body.extend(("Content-Type: %s\r\n\r\n" % content_type).encode())
        body.extend(value)
        body.extend(b"\r\n")
    body.extend(("--%s--\r\n" % boundary).encode())
    return bytes(body), {"Content-Type": "multipart/form-data; boundary=" + boundary}


def metadata(capture_id: str, title: str, client: str, image_path: Path):
    return {
        "schema_version": 1,
        "capture_id": capture_id,
        "client_id": client,
        "client_name": "test-" + client,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "capture_mode": "frozen_region",
        "platform": "linux",
        "compositor": "hyprland",
        # Deliberately wrong: the server must persist actual PNG dimensions.
        "image": {"format": "png", "width": 1, "height": 1},
        "application": {"title": title},
    }


def upload(base: str, image: Path, title: str, client: str, capture_id=None, token=TOKEN):
    capture_id = capture_id or str(uuid.uuid4())
    body, headers = multipart(metadata(capture_id, title, client, image), image)
    return capture_id, request(base, "/api/v1/captures", token, "POST", body, headers)


def chunked_upload(port: int, image: Path, title: str):
    capture_id = str(uuid.uuid4())
    body, headers = multipart(metadata(capture_id, title, "chunked-client", image), image)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        chunks = (body[position:position + 7919] for position in range(0, len(body), 7919))
        connection.request(
            "POST", "/api/v1/captures", body=chunks,
            headers={"Authorization": "Bearer " + TOKEN, **headers}, encode_chunked=True,
        )
        response = connection.getresponse()
        value = json.loads(response.read())
        return capture_id, response.status, value
    finally:
        connection.close()


def oversized_preflight(port: int) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.putrequest("POST", "/api/v1/captures")
        connection.putheader("Authorization", "Bearer " + TOKEN)
        connection.putheader("Content-Type", "multipart/form-data; boundary=unused")
        connection.putheader("Content-Length", str(536870912 + 1))
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def wait_capture(base: str, capture_id: str, timeout=120):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status, value, _ = request(base, "/api/v1/captures/" + capture_id)
        assert status == 200, value
        last = value
        ocr_done = value["latest_ocr_run"] and value["latest_ocr_run"]["status"] in ("succeeded", "failed")
        thumbnail_done = value["thumbnail_status"] in ("ready", "failed")
        translation_done = value["latest_translation_run"] is None or value["latest_translation_run"]["status"] in ("succeeded", "failed")
        if ocr_done and thumbnail_done and translation_done:
            return value
        time.sleep(0.25)
    raise AssertionError("capture did not finish: %r" % last)


def main() -> int:
    assert PYTHON.is_file(), "run scripts/build.sh first"
    assert HELPER.is_file(), "native helper is not built"
    server_command = [SERVER] if SERVER else [str(PYTHON), "-m", "scanocr_server"]
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="scanocr-integration-") as directory:
        temp = Path(directory)
        secret = temp / "token-file"
        secret.write_text(TOKEN + "\n")
        secret.chmod(0o600)
        managed_config = temp / "managed.toml"
        managed_config.write_text(
            '[server]\nmanaged = true\n[auth]\ntoken_file = "%s"\n' % secret
        )
        managed = Config.load(managed_config)
        managed.validate_permissions()
        assert managed.token == TOKEN and managed.managed and not managed.token_inline
        secret.chmod(0o644)
        try:
            managed.validate_permissions()
        except ConfigError:
            pass
        else:
            raise AssertionError("group-readable token_file was accepted")
        secret.chmod(0o600)

        config = temp / "server.toml"
        config.write_text(
            '[server]\nhost = "127.0.0.1"\nport = %d\nopen_browser = false\nmax_upload_bytes = 536870912\n'
            '[auth]\ntoken = "%s"\n'
            '[defaults]\nocr_engine = "vision"\ntranslation_engine = "apple-translation"\n'
            'source_language = "ja"\ntarget_language = "zh-Hans"\n'
            '[paths]\ndata_dir = "%s"\nhelper = "%s"\n'
            % (port, TOKEN, temp / "data", HELPER)
        )
        config.chmod(0o600)
        first_image, second_image = make_test_images(temp)
        base = "http://127.0.0.1:%d" % port
        doctor = subprocess.run(
            server_command + ["--config", str(config), "doctor"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
        )
        assert doctor.returncode == 0, doctor.stdout
        process = subprocess.Popen(
            server_command + ["--config", str(config), "serve", "--no-open-browser"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            for _ in range(300):
                status, _, _ = request(base, "/api/v1/engines")
                if status == 200:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("server did not start")

            assert request(base, "/api/v1/applications", token=None)[0] == 401
            assert request(base, "/api/v1/applications", token="wrong-token")[0] == 401
            assert request(base, "/api/v1/applications", token="correct-token-2026x")[0] == 401
            assert oversized_preflight(port) == 413

            bad_image = temp / "not-a-png.png"
            bad_image.write_bytes(b"this is not a png")
            bad_body, bad_headers = multipart(metadata(str(uuid.uuid4()), "Bad Signature", "bad", bad_image), bad_image)
            assert request(base, "/api/v1/captures", method="POST", body=bad_body, headers=bad_headers)[0] == 415
            extra = metadata(str(uuid.uuid4()), "Extra Metadata", "bad", first_image)
            extra["application"]["pid"] = 42
            extra_body, extra_headers = multipart(extra, first_image)
            assert request(base, "/api/v1/captures", method="POST", body=extra_body, headers=extra_headers)[0] == 422

            first_id, first_upload = upload(base, first_image, "Example Game", "client-a")
            same_title_id, same_upload = upload(base, second_image, "Example Game", "client-b")
            distinct_id, distinct_upload = upload(base, first_image, "Example Game: Chapter 2", "client-a")
            empty_id, empty_upload = upload(base, second_image, "", "client-c")
            chunked_id, chunked_status, chunked_response = chunked_upload(port, first_image, "Chunked Upload")
            assert chunked_status == 202 and chunked_response["thumbnail_status"] == "pending", chunked_response
            for response in (first_upload, same_upload, distinct_upload, empty_upload):
                assert response[0] == 202, response
                assert response[1]["thumbnail_status"] == "pending", response
            assert first_upload[1]["application_id"] == same_upload[1]["application_id"]
            assert first_upload[1]["application_id"] != distinct_upload[1]["application_id"]
            assert empty_upload[1]["application_id"] not in (first_upload[1]["application_id"], distinct_upload[1]["application_id"])

            wrong_id, wrong = upload(base, first_image, "Must Not Exist", "client-x", token="wrong-token")
            assert wrong[0] == 401, wrong

            replay_id, replay = upload(base, first_image, "A changed title is ignored by idempotency", "client-z", first_id)
            assert replay[0] == 202 and replay[1]["idempotent_replay"] is True, replay
            assert replay[1]["application_id"] == first_upload[1]["application_id"]

            first = wait_capture(base, first_id)
            same = wait_capture(base, same_title_id)
            distinct = wait_capture(base, distinct_id)
            empty = wait_capture(base, empty_id)
            assert (first["image_width"], first["image_height"]) == (1392, 320)
            assert (same["image_width"], same["image_height"]) == (800, 1200)
            for item in (first, same, distinct, empty):
                assert item["thumbnail_status"] == "ready", item
                assert item["latest_ocr_run"]["status"] == "succeeded", item
                assert item["ocr"]["text"].strip(), item
                translation_run = item["latest_translation_run"]
                if ALLOW_MISSING_TRANSLATION and translation_run["status"] == "failed":
                    assert translation_run["error_code"] == "model_not_installed", item
                else:
                    assert translation_run["status"] == "succeeded", item
                    assert item["translation"]["text"].strip(), item
            assert "カリン" in first["ocr"]["text"], first["ocr"]["text"]

            thumb_status, thumb, thumb_headers = request(base, "/api/v1/captures/%s/thumbnail" % first_id)
            assert thumb_status == 200 and thumb[:4] == b"RIFF" and thumb[8:12] == b"WEBP"
            assert "immutable" in thumb_headers["Cache-Control"]

            application_id = first_upload[1]["application_id"]
            status, updated, _ = request(base, "/api/v1/applications/%s/settings" % application_id, method="PUT", body={"display_name": "Renamed Game"})
            assert status == 200 and updated["source_title"] == "Example Game" and updated["display_name"] == "Renamed Game"
            third_id, third_upload = upload(base, first_image, "Example Game", "client-c")
            assert third_upload[1]["application_id"] == application_id

            apps_status, apps, _ = request(base, "/api/v1/applications")
            assert apps_status == 200
            by_title = {item["source_title"]: item for item in apps["applications"]}
            assert "Must Not Exist" not in by_title
            assert "Bad Signature" not in by_title and "Extra Metadata" not in by_title
            assert set(by_title) == {"Example Game", "Example Game: Chapter 2", "Chunked Upload", ""}
            assert by_title["Example Game"]["capture_count"] == 3
            assert by_title["Example Game"]["display_name"] == "Renamed Game"

            delete_status, _, _ = request(base, "/api/v1/captures/" + distinct_id, method="DELETE")
            assert delete_status == 204
            assert request(base, "/api/v1/captures/" + distinct_id)[0] == 404

            summary = {
                "applications": {key if key else "<empty>": value["capture_count"] for key, value in by_title.items()},
                "titles_grouped": first_upload[1]["application_id"] == same_upload[1]["application_id"],
                "wrong_token_rejected": wrong[0] == 401,
                "idempotent_replay": replay[1]["idempotent_replay"],
                "ocr_horizontal": first["ocr"]["text"],
                "ocr_vertical_preview": same["ocr"]["text"][:180],
                "translation_horizontal": first["latest_translation_run"]["status"] if first["latest_translation_run"] else "not-created",
                "translation_error": first["latest_translation_run"].get("error_code") if first["latest_translation_run"] else None,
                "thumbnail": "webp",
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        finally:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            if process.returncode not in (0, -15):
                print("--- server output ---\n" + output, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
