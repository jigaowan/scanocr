from __future__ import annotations

import os
import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


class ConfigError(ValueError):
    pass


def _parse_value(raw: str) -> Any:
    value = raw.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError as error:
            raise ConfigError("invalid TOML string: %s" % error)
    if value in ("true", "false"):
        return value == "true"
    try:
        return int(value)
    except ValueError:
        raise ConfigError("unsupported TOML value: %s" % raw)


def read_toml(path: Path) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    section: Dict[str, Any] = result.setdefault("root", {})
    for number, source_line in enumerate(path.read_text("utf-8").splitlines(), 1):
        line = source_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name:
                raise ConfigError("empty section at line %d" % number)
            section = result.setdefault(name, {})
            continue
        if "=" not in line:
            raise ConfigError("invalid config line %d" % number)
        key, raw = line.split("=", 1)
        section[key.strip()] = _parse_value(raw)
    return result


@dataclass(frozen=True)
class Config:
    path: Path
    host: str
    port: int
    open_browser: bool
    max_upload_bytes: int
    token: str
    token_file: Optional[Path]
    token_inline: bool
    managed: bool
    data_dir: Path
    helper: Path
    ocr_engine: str
    translation_engine: str
    source_language: str
    target_language: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.is_file():
            raise ConfigError("config file does not exist: %s" % path)
        doc = read_toml(path)
        server = doc.get("server", {})
        auth = doc.get("auth", {})
        defaults = doc.get("defaults", {})
        paths = doc.get("paths", {})
        inline_token = str(auth.get("token", ""))
        token_file_value = auth.get("token_file")
        if inline_token and token_file_value:
            raise ConfigError("configure only one of auth.token or auth.token_file")
        token_file: Optional[Path] = None
        if token_file_value:
            token_file = Path(os.path.expanduser(str(token_file_value)))
            if not token_file.is_absolute():
                token_file = (path.parent / token_file).resolve()
            try:
                token = token_file.read_text("utf-8").strip()
            except OSError as error:
                raise ConfigError("unable to read auth.token_file %s: %s" % (token_file, error))
        else:
            token = inline_token
        if not token or token == "replace-with-a-random-token":
            raise ConfigError("auth.token must be configured")
        max_upload = int(server.get("max_upload_bytes", 536870912))
        if max_upload <= 0:
            raise ConfigError("server.max_upload_bytes must be a positive integer")
        port = int(server.get("port", 8732))
        if not 1 <= port <= 65535:
            raise ConfigError("server.port must be between 1 and 65535")
        default_data = Path.home() / "Library" / "Application Support" / "ScanOCR"
        if getattr(sys, "frozen", False):
            bundle_root = Path(sys.executable).resolve().parent.parent
            helper_default = bundle_root / "libexec" / "scanocr-native-helper"
        else:
            repo_root = Path(__file__).resolve().parent.parent
            helper_default = repo_root / ".build" / "scanocr-native-helper"
        return cls(
            path=path.resolve(),
            host=str(server.get("host", "127.0.0.1")),
            port=port,
            open_browser=bool(server.get("open_browser", True)),
            max_upload_bytes=max_upload,
            token=token,
            token_file=token_file,
            token_inline=not bool(token_file_value),
            managed=bool(server.get("managed", False)),
            data_dir=Path(os.path.expanduser(str(paths.get("data_dir", default_data)))).resolve(),
            helper=Path(os.path.expanduser(str(paths.get("helper", helper_default)))).resolve(),
            ocr_engine=str(defaults.get("ocr_engine", "vision")),
            translation_engine=str(defaults.get("translation_engine", "apple-translation")),
            source_language=str(defaults.get("source_language", "auto")),
            target_language=str(defaults.get("target_language", "")),
        )

    def validate_permissions(self) -> None:
        if self.token_inline:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & 0o077:
                raise ConfigError("config contains a token and must be mode 0600 (currently %04o)" % mode)
        if self.token_file:
            mode = stat.S_IMODE(self.token_file.stat().st_mode)
            if mode & 0o077:
                raise ConfigError("auth.token_file must be mode 0600 (currently %04o)" % mode)
