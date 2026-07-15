package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
)

var errCaptureCanceled = errors.New("capture canceled")

type activeWindow struct {
	Title    *string `json:"title"`
	StableID string  `json:"stableId"`
}

type workspace struct {
	ID            int  `json:"id"`
	HasFullscreen bool `json:"hasfullscreen"`
}

type monitorWorkspace struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

type monitor struct {
	ActiveWorkspace  monitorWorkspace `json:"activeWorkspace"`
	SpecialWorkspace monitorWorkspace `json:"specialWorkspace"`
}

type clientWindow struct {
	At         [2]int           `json:"at"`
	Size       [2]int           `json:"size"`
	StableID   string           `json:"stableId"`
	Workspace  monitorWorkspace `json:"workspace"`
	Fullscreen int              `json:"fullscreen"`
}

func commandOutput(ctx context.Context, stdin io.Reader, name string, args ...string) ([]byte, error) {
	command := exec.CommandContext(ctx, name, args...)
	command.Stdin = stdin
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		message := strings.TrimSpace(stderr.String())
		if message != "" {
			return nil, fmt.Errorf("%s: %w", message, err)
		}
		return nil, err
	}
	return stdout.Bytes(), nil
}

func hyprctlJSON(ctx context.Context, result any, args ...string) error {
	arguments := append(append([]string{}, args...), "-j")
	data, err := commandOutput(ctx, nil, "hyprctl", arguments...)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(data, result); err != nil {
		return fmt.Errorf("decode hyprctl %s JSON: %w", strings.Join(args, " "), err)
	}
	return nil
}

func readActiveWindow(ctx context.Context) (activeWindow, error) {
	var window activeWindow
	if err := hyprctlJSON(ctx, &window, "activewindow"); err != nil {
		return activeWindow{}, fmt.Errorf("read active window: %w", err)
	}
	if window.Title == nil {
		return activeWindow{}, fmt.Errorf("active window JSON does not contain a string title")
	}
	return window, nil
}

func captureActive(ctx context.Context, window activeWindow, output string) error {
	if window.StableID == "" {
		return fmt.Errorf("active window does not have a stableId")
	}
	if _, err := commandOutput(ctx, nil, "grim", "-t", "png", "-T", window.StableID, output); err != nil {
		return fmt.Errorf("capture active window: %w", err)
	}
	return nil
}

func visibleWindows(ctx context.Context) ([]clientWindow, error) {
	var workspaces []workspace
	var monitors []monitor
	var clients []clientWindow
	if err := hyprctlJSON(ctx, &workspaces, "workspaces"); err != nil {
		return nil, err
	}
	if err := hyprctlJSON(ctx, &monitors, "monitors"); err != nil {
		return nil, err
	}
	if err := hyprctlJSON(ctx, &clients, "clients"); err != nil {
		return nil, err
	}

	fullscreenWorkspaces := make(map[int]bool)
	for _, item := range workspaces {
		if item.HasFullscreen {
			fullscreenWorkspaces[item.ID] = true
		}
	}
	visibleWorkspaces := make(map[int]bool)
	for _, item := range monitors {
		if item.SpecialWorkspace.Name == "" {
			visibleWorkspaces[item.ActiveWorkspace.ID] = true
		} else {
			visibleWorkspaces[item.SpecialWorkspace.ID] = true
		}
	}

	visible := make([]clientWindow, 0, len(clients))
	for _, item := range clients {
		if item.StableID == "" || item.Size[0] <= 0 || item.Size[1] <= 0 {
			continue
		}
		if (visibleWorkspaces[item.Workspace.ID] && !fullscreenWorkspaces[item.Workspace.ID]) || item.Fullscreen > 0 {
			visible = append(visible, item)
		}
	}
	return visible, nil
}

func windowSelection(window clientWindow) string {
	return fmt.Sprintf("%d,%d %dx%d|%s", window.At[0], window.At[1], window.Size[0], window.Size[1], window.StableID)
}

type childProcess struct {
	command *exec.Cmd
	done    chan struct{}
	waitErr error
	once    sync.Once
}

func startFrozenScreen(ctx context.Context) (*childProcess, error) {
	// Stop owns the whole process group. Using CommandContext here would let Go
	// kill only the direct child first, potentially leaving helper children
	// behind before Stop gets a chance to reap the group.
	command := exec.Command("hyprpicker", "-rz")
	command.Stdout = io.Discard
	command.Stderr = io.Discard
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := command.Start(); err != nil {
		return nil, err
	}
	process := &childProcess{command: command, done: make(chan struct{})}
	go func() {
		process.waitErr = command.Wait()
		close(process.done)
	}()
	select {
	case <-process.done:
		if process.waitErr != nil {
			return nil, fmt.Errorf("hyprpicker exited during startup: %w", process.waitErr)
		}
		return nil, fmt.Errorf("hyprpicker exited during startup")
	case <-time.After(200 * time.Millisecond):
		return process, nil
	case <-ctx.Done():
		process.Stop()
		return nil, ctx.Err()
	}
}

func (process *childProcess) Stop() {
	if process == nil {
		return
	}
	process.once.Do(func() {
		select {
		case <-process.done:
			return
		default:
		}
		_ = syscall.Kill(-process.command.Process.Pid, syscall.SIGTERM)
		select {
		case <-process.done:
		case <-time.After(2 * time.Second):
			_ = syscall.Kill(-process.command.Process.Pid, syscall.SIGKILL)
			<-process.done
		}
	})
}

func captureArea(ctx context.Context, output string) error {
	process, err := startFrozenScreen(ctx)
	if err != nil {
		return fmt.Errorf("start frozen screen: %w", err)
	}
	defer process.Stop()

	windows, err := visibleWindows(ctx)
	if err != nil {
		return fmt.Errorf("enumerate visible windows: %w", err)
	}
	var input strings.Builder
	selections := make(map[string]string, len(windows))
	for _, window := range windows {
		selection := windowSelection(window)
		fmt.Fprintf(&input, "%d,%d %dx%d %s\n", window.At[0], window.At[1], window.Size[0], window.Size[1], window.StableID)
		selections[selection] = window.StableID
	}

	data, err := commandOutput(ctx, strings.NewReader(input.String()), "slurp", "-o", "-f", "%x,%y %wx%h|%l")
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return errCaptureCanceled
	}
	choice := strings.TrimSpace(string(data))
	if choice == "" {
		return errCaptureCanceled
	}
	if stableID := selections[choice]; stableID != "" {
		if _, err := commandOutput(ctx, nil, "grim", "-t", "png", "-T", stableID, output); err != nil {
			return fmt.Errorf("capture selected window: %w", err)
		}
		return nil
	}
	geometry, _, _ := strings.Cut(choice, "|")
	if geometry == "" {
		return errCaptureCanceled
	}
	if _, err := commandOutput(ctx, nil, "grim", "-t", "png", "-g", geometry, output); err != nil {
		return fmt.Errorf("capture selected area: %w", err)
	}
	return nil
}
