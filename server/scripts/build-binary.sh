#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${SCANOCR_VERSION:-}
if [ -z "$VERSION" ]; then
  VERSION=$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || true)
fi
if [ -z "$VERSION" ]; then
  VERSION=$(awk -F '"' '/^version = / { print $2; exit }' "$ROOT/pyproject.toml")
fi
VERSION=${VERSION#v}
NAME="scanocr-server-$VERSION-aarch64-darwin"
WORK="$ROOT/.release"
STAGE="$WORK/stage/$NAME"
DIST="$ROOT/dist"

test "$(uname -m)" = "arm64" || {
  echo "binary releases must be built natively on Apple Silicon" >&2
  exit 1
}

"$ROOT/scripts/build.sh"
"$ROOT/.venv/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements-build.txt"

rm -rf "$WORK" "$DIST"
mkdir -p "$WORK/pyinstaller" "$STAGE/bin" "$STAGE/libexec" "$DIST"

"$ROOT/.venv/bin/pyinstaller" \
  --clean \
  --noconfirm \
  --onefile \
  --noupx \
  --target-architecture arm64 \
  --name scanocr-server \
  --distpath "$WORK/pyinstaller/dist" \
  --workpath "$WORK/pyinstaller/work" \
  --specpath "$WORK/pyinstaller" \
  --paths "$ROOT" \
  --add-data "$ROOT/scanocr_server/web:scanocr_server/web" \
  "$ROOT/packaging/entrypoint.py"

install -m755 "$WORK/pyinstaller/dist/scanocr-server" "$STAGE/bin/scanocr-server"
install -m755 "$ROOT/.build/scanocr-native-helper" "$STAGE/libexec/scanocr-native-helper"
/usr/bin/codesign --force --sign - "$STAGE/bin/scanocr-server"
/usr/bin/codesign --force --sign - "$STAGE/libexec/scanocr-native-helper"

file "$STAGE/bin/scanocr-server" "$STAGE/libexec/scanocr-native-helper" | grep -q 'arm64'
"$STAGE/bin/scanocr-server" --help >/dev/null
printf '{"operation":"capabilities"}' | "$STAGE/libexec/scanocr-native-helper" >/dev/null

tar -czf "$DIST/$NAME.tar.gz" -C "$WORK/stage" "$NAME"
(cd "$DIST" && shasum -a 256 "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "$DIST/$NAME.tar.gz"
