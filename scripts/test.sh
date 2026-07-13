#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHONPATH="$ROOT" exec "$ROOT/.venv/bin/python" "$ROOT/tests/integration_test.py"
