#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$ROOT/.build"
cd "$ROOT"
CGO_ENABLED=${CGO_ENABLED:-0}
export CGO_ENABLED
VERSION=${SCANOCR_VERSION:-$("$ROOT/scripts/version.sh")}
exec go build -trimpath -ldflags "-X main.clientVersion=$VERSION" -o "$ROOT/.build/scanocr-client" .
