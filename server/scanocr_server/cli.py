from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
import threading
import webbrowser
from pathlib import Path

from .app import create_server
from .config import Config, ConfigError
from .state import ServerState


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="scanocr-server")
    result.add_argument("--config", type=Path, default=Path.home() / ".config" / "scanocr" / "server.toml")
    subcommands = result.add_subparsers(dest="command", required=True)
    serve = subcommands.add_parser("serve")
    serve.add_argument("--no-open-browser", action="store_true")
    subcommands.add_parser("doctor")
    return result


def load_config(path: Path) -> Config:
    config = Config.load(path)
    config.validate_permissions()
    return config


def doctor(config: Config) -> int:
    checks = {
        "platform": {"ok": platform.system() == "Darwin", "detail": platform.platform()},
        "config_permissions": {"ok": True, "detail": str(config.path)},
        "data_parent_writable": {
            "ok": os.access(str(config.data_dir.parent if not config.data_dir.exists() else config.data_dir), os.W_OK),
            "detail": str(config.data_dir),
        },
        "token": {"ok": bool(config.token), "detail": "configured" if config.token else "missing"},
        "native_helper": {"ok": config.helper.is_file() and os.access(str(config.helper), os.X_OK), "detail": str(config.helper)},
    }
    try:
        state = ServerState(config)
        checks["database"] = {"ok": True, "detail": str(state.db.path)}
        checks["engines"] = {
            "ok": all(item.available for item in state.registry.engines.values()),
            "detail": {key: value.as_dict() for key, value in state.registry.engines.items()},
        }
    except Exception as error:
        checks["startup"] = {"ok": False, "detail": str(error)}
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(item["ok"] for item in checks.values()) else 1


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as error:
        print("scanocr-server: %s" % error, file=sys.stderr)
        return 2
    if args.command == "doctor":
        return doctor(config)
    state = ServerState(config)
    server = create_server(state)
    url = "http://%s:%d/" % (config.host, config.port)
    logging.info("ScanOCR listening at %s", url)
    if config.open_browser and not args.no_open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_event.set()
        server.server_close()
    return 0
