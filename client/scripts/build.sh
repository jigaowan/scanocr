#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$ROOT/.build"
cd "$ROOT"
CGO_ENABLED=${CGO_ENABLED:-0}
export CGO_ENABLED
exec go build -trimpath -o "$ROOT/.build/scanocr-client" .
