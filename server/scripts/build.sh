#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${SCANOCR_VERSION:-$("$ROOT/scripts/version.sh")}
mkdir -p "$ROOT/.build"
CLANG_MODULE_CACHE_PATH="$ROOT/.build/ModuleCache" \
SWIFT_MODULE_CACHE_PATH="$ROOT/.build/ModuleCache" \
xcrun swiftc \
  -parse-as-library \
  -target "$(uname -m)-apple-macosx26.0" \
  "$ROOT/native/ScanOCRNativeHelper.swift" \
  -o "$ROOT/.build/scanocr-native-helper"
python3 -m venv "$ROOT/.venv"
SCANOCR_VERSION="$VERSION" \
  "$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check "$ROOT"
