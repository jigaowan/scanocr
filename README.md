# ScanOCR

ScanOCR consists of a macOS OCR/translation server and a Linux/Hyprland screenshot client:

```text
client/  Go client, client config, build and test scripts
server/  Python server, Swift native helper, packaging and integration tests
docs/    Protocol and implementation design documents
```

The server implements [`docs/server-design.md`](docs/server-design.md). Its HTTP/SQLite/queue layer is Python 3.9 compatible; Apple Vision and Translation Framework access is isolated in a Swift helper.

Requires Apple Silicon and macOS 26 or newer. The helper uses Translation Framework's headless installed-model session API.

## Build and run

```sh
server/scripts/build.sh
mkdir -p ~/.config/scanocr
cp server/config.example.toml ~/.config/scanocr/server.toml
# Edit auth.token and defaults.target_language.
server/.venv/bin/scanocr-server --config ~/.config/scanocr/server.toml doctor
server/.venv/bin/scanocr-server --config ~/.config/scanocr/server.toml serve
```

The default data directory is `~/Library/Application Support/ScanOCR`. The service accepts normal config-file permissions. When `auth.token_file` is used, that separate secret file must remain mode `0600`.

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
server/scripts/test.sh
```

The integration test generates two images in its temporary directory, uploads them with repeated, distinct, empty, and chunked-upload titles, exercises valid/invalid tokens and idempotent capture IDs, and waits for real Vision OCR, Apple Translation, and WebP thumbnail results.

## Hyprland client

The client is a Go binary and only supports Linux/Hyprland. Build it with an installed Go toolchain or a temporary Nix shell:

```sh
nix shell nixpkgs#go --command client/scripts/build.sh
mkdir -p ~/.config/scanocr
cp client/config.example.toml ~/.config/scanocr/client.toml
chmod 600 ~/.config/scanocr/client.toml
# Edit server_url, token, and client_name.
client/.build/scanocr-client doctor
client/.build/scanocr-client capture active
client/.build/scanocr-client capture area
```

At runtime, active-window capture requires `hyprctl` and `grim`; area capture additionally requires `slurp` and `hyprpicker`. `notify-send` is required when notifications are enabled. The client does not require Go after it has been built.

The screenshot tools can also be supplied temporarily with Nix:

```sh
nix shell nixpkgs#grim nixpkgs#slurp nixpkgs#hyprpicker nixpkgs#libnotify \
  --command client/.build/scanocr-client doctor
```

Run the client tests with:

```sh
nix shell nixpkgs#go --command client/scripts/test.sh
```

## Binary release

GitHub Actions builds the Apple Silicon binary bundle on a native `macos-26` runner. Tagged builds publish these two release assets:

- `scanocr-server-<version>-aarch64-darwin.tar.gz`
- `scanocr-server-<version>-aarch64-darwin.tar.gz.sha256`

The archive contains a standalone server executable and the native Vision/Translation helper:

```text
bin/scanocr-server
libexec/scanocr-native-helper
```

Run `server/scripts/build-binary.sh` to produce the same archive under `server/dist/`. Building requires Xcode; running the archive does not.
