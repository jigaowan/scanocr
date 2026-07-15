package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func writeExecutable(t *testing.T, directory, name, source string) {
	t.Helper()
	path := filepath.Join(directory, name)
	if err := os.WriteFile(path, []byte(source), 0o755); err != nil {
		t.Fatal(err)
	}
}

func writeTestPNG(t *testing.T, path string) {
	t.Helper()
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	value := image.NewRGBA(image.Rect(0, 0, 2, 3))
	value.Set(0, 0, color.RGBA{R: 255, A: 255})
	if err := png.Encode(file, value); err != nil {
		_ = file.Close()
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
}

func installFakeCommands(t *testing.T, directory string) {
	t.Helper()
	writeExecutable(t, directory, "hyprctl", `#!/bin/sh
case "$1" in
	  activewindow) printf '%s\n' '{"title":"Game \"Quoted\"","stableId":"window-1"}' ;;
  workspaces) printf '%s\n' '[{"id":1,"hasfullscreen":false}]' ;;
  monitors) printf '%s\n' '[{"activeWorkspace":{"id":1,"name":"1"},"specialWorkspace":{"id":0,"name":""}}]' ;;
  clients) printf '%s\n' '[{"at":[0,0],"size":[800,600],"stableId":"window-1","workspace":{"id":1,"name":"1"},"fullscreen":0}]' ;;
  *) exit 1 ;;
esac
`)
	writeExecutable(t, directory, "grim", `#!/bin/sh
for output do :; done
cp "$SCANOCR_TEST_IMAGE" "$output"
`)
}

func prepareCaptureEnvironment(t *testing.T) (string, Config) {
	t.Helper()
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	if err := os.Mkdir(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	installFakeCommands(t, bin)
	pngPath := filepath.Join(root, "fixture.png")
	writeTestPNG(t, pngPath)
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("SCANOCR_TEST_IMAGE", pngPath)
	t.Setenv("XDG_RUNTIME_DIR", filepath.Join(root, "runtime"))
	t.Setenv("XDG_STATE_HOME", filepath.Join(root, "state"))
	t.Setenv("HYPRLAND_INSTANCE_SIGNATURE", "test-session")
	if err := os.Mkdir(os.Getenv("XDG_RUNTIME_DIR"), 0o700); err != nil {
		t.Fatal(err)
	}
	return root, Config{Token: "test-token", ClientName: "test-client", Notify: false}
}

func TestClientVersion(t *testing.T) {
	if clientVersion == "" {
		t.Fatal("client version is empty")
	}
}

func TestLoadConfig(t *testing.T) {
	path := filepath.Join(t.TempDir(), "client.toml")
	content := "server_url = \"http://127.0.0.1:8732\"\ntoken = \"secret\"\nclient_name = \"gaming-pc\"\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	config, err := loadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if config.ServerURL != "http://127.0.0.1:8732" || config.Token != "secret" || !config.Notify {
		t.Fatalf("unexpected config: %#v", config)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := loadConfig(path); err == nil || !strings.Contains(err.Error(), "group or others") {
		t.Fatalf("expected permission error, got %v", err)
	}
}

func TestNewUUIDAndPersistentClientID(t *testing.T) {
	seen := make(map[string]bool)
	for range 100 {
		value, err := newUUID()
		if err != nil {
			t.Fatal(err)
		}
		if len(value) != 36 || value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' {
			t.Fatalf("invalid UUID shape: %q", value)
		}
		if value[14] != '4' || !strings.ContainsRune("89ab", rune(value[19])) {
			t.Fatalf("not a UUID v4: %q", value)
		}
		if seen[value] {
			t.Fatalf("duplicate UUID: %q", value)
		}
		seen[value] = true
	}

	t.Setenv("XDG_STATE_HOME", t.TempDir())
	first, err := loadOrCreateClientID()
	if err != nil {
		t.Fatal(err)
	}
	second, err := loadOrCreateClientID()
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("client ID changed: %q != %q", first, second)
	}
	info, err := os.Stat(filepath.Join(os.Getenv("XDG_STATE_HOME"), "scanocr", "client-id"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("client ID mode is %04o", info.Mode().Perm())
	}
}

func TestCaptureActiveUploadsCurrentServerContract(t *testing.T) {
	root, config := prepareCaptureEnvironment(t)
	metadataChannel := make(chan map[string]any, 1)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v1/captures" {
			http.NotFound(writer, request)
			return
		}
		if request.Header.Get("Authorization") != "Bearer test-token" {
			t.Errorf("unexpected authorization header")
		}
		if len(request.TransferEncoding) != 1 || request.TransferEncoding[0] != "chunked" {
			t.Errorf("expected chunked upload, got %v", request.TransferEncoding)
		}
		reader, err := request.MultipartReader()
		if err != nil {
			t.Error(err)
			return
		}
		var metadata map[string]any
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				t.Error(err)
				return
			}
			switch part.FormName() {
			case "metadata":
				if part.Header.Get("Content-Type") != "application/json" {
					t.Errorf("unexpected metadata type: %s", part.Header.Get("Content-Type"))
				}
				if err := json.NewDecoder(part).Decode(&metadata); err != nil {
					t.Error(err)
					return
				}
			case "image":
				if part.Header.Get("Content-Type") != "image/png" {
					t.Errorf("unexpected image type: %s", part.Header.Get("Content-Type"))
				}
				if _, err := png.DecodeConfig(part); err != nil {
					t.Error(err)
					return
				}
			default:
				t.Errorf("unexpected part: %s", part.FormName())
			}
		}
		metadataChannel <- metadata
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusAccepted)
		fmt.Fprintf(writer, `{"capture_id":%q,"application_id":"app","status":"queued","thumbnail_status":"pending","idempotent_replay":false}`, metadata["capture_id"])
	}))
	defer server.Close()
	config.ServerURL = server.URL
	var stdout bytes.Buffer
	app := NewApp(config, &stdout, io.Discard)
	if err := app.Capture(context.Background(), "active"); err != nil {
		t.Fatal(err)
	}
	metadata := <-metadataChannel
	if metadata["capture_mode"] != "active_window" || metadata["platform"] != "linux" || metadata["compositor"] != "hyprland" {
		t.Fatalf("unexpected metadata: %#v", metadata)
	}
	imageValue := metadata["image"].(map[string]any)
	if imageValue["width"] != float64(2) || imageValue["height"] != float64(3) {
		t.Fatalf("unexpected dimensions: %#v", imageValue)
	}
	application := metadata["application"].(map[string]any)
	if len(application) != 1 || application["title"] != `Game "Quoted"` {
		t.Fatalf("unexpected application: %#v", application)
	}
	if !strings.Contains(stdout.String(), "capture submitted:") {
		t.Fatalf("missing success output: %q", stdout.String())
	}
	entries, err := os.ReadDir(filepath.Join(root, "runtime", "scanocr-client"))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			t.Fatalf("capture directory was not removed: %s", entry.Name())
		}
	}
}

func TestCaptureAreaCancellationCleansFrozenScreen(t *testing.T) {
	root, config := prepareCaptureEnvironment(t)
	pickerPIDPath := filepath.Join(root, "picker.pid")
	t.Setenv("SCANOCR_TEST_PICKER_PID", pickerPIDPath)
	bin := filepath.Join(root, "bin")
	writeExecutable(t, bin, "hyprpicker", `#!/bin/sh
echo $$ > "$SCANOCR_TEST_PICKER_PID"
trap 'exit 0' TERM INT HUP
while :; do sleep 1; done
`)
	writeExecutable(t, bin, "slurp", "#!/bin/sh\nexit 1\n")
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		requests.Add(1)
		writer.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	config.ServerURL = server.URL
	app := NewApp(config, io.Discard, io.Discard)
	if err := app.Capture(context.Background(), "area"); err != nil {
		t.Fatal(err)
	}
	if requests.Load() != 0 {
		t.Fatalf("canceled capture made %d requests", requests.Load())
	}
	data, err := os.ReadFile(pickerPIDPath)
	if err != nil {
		t.Fatal(err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Kill(pid, 0); err == nil {
		t.Fatalf("hyprpicker process %d is still running", pid)
	}
}

func TestCaptureLockIsNonBlocking(t *testing.T) {
	_, config := prepareCaptureEnvironment(t)
	server := httptest.NewServer(http.NotFoundHandler())
	defer server.Close()
	config.ServerURL = server.URL
	base, err := runtimeBase()
	if err != nil {
		t.Fatal(err)
	}
	lock, err := acquireCaptureLock(filepath.Join(base, "capture.lock"))
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	app := NewApp(config, io.Discard, io.Discard)
	if err := app.Capture(context.Background(), "active"); !errors.Is(err, errCaptureBusy) {
		t.Fatalf("expected capture busy, got %v", err)
	}
}

func TestUploadDoesNotHoldCaptureLock(t *testing.T) {
	_, config := prepareCaptureEnvironment(t)
	firstEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		reader, err := request.MultipartReader()
		if err != nil {
			http.Error(writer, err.Error(), http.StatusBadRequest)
			return
		}
		var metadata captureMetadata
		for {
			part, err := reader.NextPart()
			if err == io.EOF {
				break
			}
			if err != nil {
				http.Error(writer, err.Error(), http.StatusBadRequest)
				return
			}
			if part.FormName() == "metadata" {
				if err := json.NewDecoder(part).Decode(&metadata); err != nil {
					http.Error(writer, err.Error(), http.StatusBadRequest)
					return
				}
			} else if _, err := io.Copy(io.Discard, part); err != nil {
				http.Error(writer, err.Error(), http.StatusBadRequest)
				return
			}
		}
		if requests.Add(1) == 1 {
			close(firstEntered)
			<-releaseFirst
		}
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusAccepted)
		fmt.Fprintf(writer, `{"capture_id":%q,"application_id":"app","status":"queued","thumbnail_status":"pending","idempotent_replay":false}`, metadata.CaptureID)
	}))
	defer server.Close()
	config.ServerURL = server.URL
	app := NewApp(config, io.Discard, io.Discard)

	firstResult := make(chan error, 1)
	go func() {
		firstResult <- app.Capture(context.Background(), "active")
	}()
	select {
	case <-firstEntered:
	case err := <-firstResult:
		close(releaseFirst)
		t.Fatalf("first capture failed before upload: %v", err)
	case <-time.After(5 * time.Second):
		close(releaseFirst)
		t.Fatal("first upload did not reach server")
	}
	secondErr := app.Capture(context.Background(), "active")
	close(releaseFirst)
	if secondErr != nil {
		t.Fatalf("second capture was blocked by first upload: %v", secondErr)
	}
	if err := <-firstResult; err != nil {
		t.Fatalf("first capture failed: %v", err)
	}
	if requests.Load() != 2 {
		t.Fatalf("expected two uploads, got %d", requests.Load())
	}
}

func TestInvalidScreenshotIsNotUploaded(t *testing.T) {
	root, config := prepareCaptureEnvironment(t)
	invalid := filepath.Join(root, "invalid.png")
	if err := os.WriteFile(invalid, []byte("not a PNG"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SCANOCR_TEST_IMAGE", invalid)
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		requests.Add(1)
		writer.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	config.ServerURL = server.URL
	app := NewApp(config, io.Discard, io.Discard)
	err := app.Capture(context.Background(), "active")
	if err == nil || !strings.Contains(err.Error(), "validate screenshot PNG") {
		t.Fatalf("unexpected error: %v", err)
	}
	if requests.Load() != 0 {
		t.Fatalf("invalid screenshot made %d requests", requests.Load())
	}
}

func TestUploadReportsPayloadTooLarge(t *testing.T) {
	root := t.TempDir()
	imagePath := filepath.Join(root, "capture.png")
	writeTestPNG(t, imagePath)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusRequestEntityTooLarge)
		_, _ = writer.Write([]byte(`{"error":{"code":"payload_too_large","message":"too large"}}`))
	}))
	defer server.Close()
	metadata := captureMetadata{CaptureID: "capture-id"}
	_, err := uploadCapture(context.Background(), newHTTPClient(), Config{ServerURL: server.URL, Token: "token"}, metadata, imagePath)
	if err == nil || !strings.Contains(err.Error(), "upload limit") {
		t.Fatalf("unexpected error: %v", err)
	}
}
