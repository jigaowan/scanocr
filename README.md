# ScanOCR macOS server

macOS implementation of [`docs/server-design.md`](docs/server-design.md). The HTTP/SQLite/queue layer is Python 3.9 compatible; Apple Vision and Translation Framework access is isolated in a Swift helper.

Requires Apple Silicon and macOS 26 or newer. The helper uses Translation Framework's headless installed-model session API.

## Build and run

```sh
scripts/build.sh
mkdir -p ~/.config/scanocr
cp config.example.toml ~/.config/scanocr/server.toml
chmod 600 ~/.config/scanocr/server.toml
# Edit auth.token and defaults.target_language.
.venv/bin/scanocr-server --config ~/.config/scanocr/server.toml doctor
.venv/bin/scanocr-server --config ~/.config/scanocr/server.toml serve
```

The default data directory is `~/Library/Application Support/ScanOCR`. The service refuses to start when the token is missing or the config is readable by group/other users.

## Capture request

```sh
curl -X POST http://127.0.0.1:8732/api/v1/captures \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -F 'metadata=@metadata.json;type=application/json' \
  -F 'image=@capture.png;type=image/png'
```

The response is `202 Accepted`; thumbnail, OCR and translation run asynchronously. All `/api/v1/*` routes require the same Bearer token.

## Tests

```sh
scripts/test.sh
```

The integration test generates two images in its temporary directory, uploads them with repeated, distinct, empty, and chunked-upload titles, exercises valid/invalid tokens and idempotent capture IDs, and waits for real Vision OCR, Apple Translation, and WebP thumbnail results.

## Binary release

GitHub Actions builds the Apple Silicon binary bundle on a native `macos-26` runner. Tagged builds publish these two release assets:

- `scanocr-server-<version>-aarch64-darwin.tar.gz`
- `scanocr-server-<version>-aarch64-darwin.tar.gz.sha256`

The archive contains a standalone server executable and the native Vision/Translation helper:

```text
bin/scanocr-server
libexec/scanocr-native-helper
```

Run `scripts/build-binary.sh` to produce the same archive locally. Building requires Xcode; running the archive does not.
