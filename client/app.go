package main

import (
	"context"
	"errors"
	"fmt"
	"image/png"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

type App struct {
	config Config
	http   *http.Client
	stdout io.Writer
	stderr io.Writer
}

func NewApp(config Config, stdout, stderr io.Writer) *App {
	return &App{config: config, http: newHTTPClient(), stdout: stdout, stderr: stderr}
}

func (app *App) notify(ctx context.Context, urgency, message string) {
	if !app.config.Notify {
		return
	}
	command := exec.CommandContext(ctx, "notify-send", "--urgency="+urgency, "--app-name=scanocr-client", "ScanOCR", message)
	command.Stdout = io.Discard
	command.Stderr = io.Discard
	_ = command.Run()
}

func (app *App) requiredCommands(mode string) error {
	commands := []string{"hyprctl", "grim"}
	if mode == "area" {
		commands = append(commands, "slurp", "hyprpicker")
	}
	if app.config.Notify {
		commands = append(commands, "notify-send")
	}
	for _, command := range commands {
		if _, err := exec.LookPath(command); err != nil {
			return fmt.Errorf("required command not found: %s", command)
		}
	}
	return nil
}

func pngSize(path string) (int, int, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, 0, err
	}
	defer file.Close()
	configuration, err := png.DecodeConfig(file)
	if err != nil {
		return 0, 0, err
	}
	if configuration.Width <= 0 || configuration.Height <= 0 {
		return 0, 0, fmt.Errorf("PNG has invalid dimensions")
	}
	return configuration.Width, configuration.Height, nil
}

func (app *App) Capture(ctx context.Context, mode string) error {
	captureMode := ""
	switch mode {
	case "active":
		captureMode = "active_window"
	case "area":
		captureMode = "frozen_region"
	default:
		return fmt.Errorf("unknown capture mode: %s", mode)
	}
	if os.Getenv("HYPRLAND_INSTANCE_SIGNATURE") == "" {
		return fmt.Errorf("HYPRLAND_INSTANCE_SIGNATURE is not set")
	}
	if err := app.requiredCommands(mode); err != nil {
		return err
	}
	base, err := runtimeBase()
	if err != nil {
		return err
	}
	lock, err := acquireCaptureLock(filepath.Join(base, "capture.lock"))
	if errors.Is(err, errCaptureBusy) {
		app.notify(ctx, "normal", "捕获进行中")
		return err
	}
	if err != nil {
		return err
	}
	lockHeld := true
	defer func() {
		if lockHeld {
			_ = lock.Close()
		}
	}()

	captureID, err := newUUID()
	if err != nil {
		return err
	}
	clientID, err := loadOrCreateClientID()
	if err != nil {
		return fmt.Errorf("load client_id: %w", err)
	}
	window, err := readActiveWindow(ctx)
	if err != nil {
		return err
	}
	capturedAt := time.Now().UTC().Format(time.RFC3339Nano)
	directory := filepath.Join(base, captureID)
	if err := os.Mkdir(directory, 0o700); err != nil {
		return err
	}
	defer os.RemoveAll(directory)
	imagePath := filepath.Join(directory, "capture.png")

	if mode == "active" {
		err = captureActive(ctx, window, imagePath)
	} else {
		err = captureArea(ctx, imagePath)
	}
	if errors.Is(err, errCaptureCanceled) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := lock.Close(); err != nil {
		return fmt.Errorf("release capture lock: %w", err)
	}
	lockHeld = false

	width, height, err := pngSize(imagePath)
	if err != nil {
		return fmt.Errorf("validate screenshot PNG: %w", err)
	}
	metadata := captureMetadata{
		SchemaVersion: 1,
		CaptureID:     captureID,
		ClientID:      clientID,
		ClientName:    app.config.ClientName,
		CapturedAt:    capturedAt,
		CaptureMode:   captureMode,
		Platform:      "linux",
		Compositor:    "hyprland",
		Image:         imageMetadata{Format: "png", Width: width, Height: height},
		Application:   applicationMeta{Title: *window.Title},
	}
	if _, err := uploadCapture(ctx, app.http, app.config, metadata, imagePath); err != nil {
		return err
	}
	fmt.Fprintf(app.stdout, "capture submitted: %s\n", captureID)
	app.notify(ctx, "normal", "截图已提交")
	return nil
}

func (app *App) Doctor(ctx context.Context) error {
	failed := false
	commands := []string{"hyprctl", "grim", "slurp", "hyprpicker"}
	if app.config.Notify {
		commands = append(commands, "notify-send")
	}
	for _, command := range commands {
		if _, err := exec.LookPath(command); err != nil {
			fmt.Fprintf(app.stdout, "[missing] %s\n", command)
			failed = true
		} else {
			fmt.Fprintf(app.stdout, "[ok] %s\n", command)
		}
	}
	if os.Getenv("HYPRLAND_INSTANCE_SIGNATURE") == "" {
		fmt.Fprintln(app.stdout, "[missing] HYPRLAND_INSTANCE_SIGNATURE")
		failed = true
	} else {
		fmt.Fprintln(app.stdout, "[ok] Hyprland session")
	}
	if !failed {
		checks := []struct {
			name   string
			result any
		}{
			{"activewindow", &activeWindow{}},
			{"workspaces", &[]workspace{}},
			{"monitors", &[]monitor{}},
			{"clients", &[]clientWindow{}},
		}
		for _, check := range checks {
			if err := hyprctlJSON(ctx, check.result, check.name); err != nil {
				fmt.Fprintf(app.stdout, "[failed] hyprctl %s -j: %v\n", check.name, err)
				failed = true
			} else {
				fmt.Fprintf(app.stdout, "[ok] hyprctl %s -j\n", check.name)
			}
		}
	}
	base, err := runtimeBase()
	if err != nil {
		fmt.Fprintf(app.stdout, "[failed] runtime directory: %v\n", err)
		failed = true
	} else {
		lock, err := acquireCaptureLock(filepath.Join(base, "doctor.lock"))
		if err != nil {
			fmt.Fprintf(app.stdout, "[failed] runtime lock: %v\n", err)
			failed = true
		} else {
			_ = lock.Close()
			fmt.Fprintln(app.stdout, "[ok] runtime directory and lock")
		}
	}
	if err := checkServer(ctx, app.http, app.config); err != nil {
		fmt.Fprintf(app.stdout, "[failed] server connection and token: %v\n", err)
		failed = true
	} else {
		fmt.Fprintln(app.stdout, "[ok] server connection and token")
	}
	if failed {
		return fmt.Errorf("doctor found problems")
	}
	return nil
}
