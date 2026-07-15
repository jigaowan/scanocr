package main

import (
	"crypto/rand"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

var errCaptureBusy = errors.New("capture already in progress")

type captureLock struct {
	file *os.File
}

func runtimeBase() (string, error) {
	root := os.Getenv("XDG_RUNTIME_DIR")
	if root == "" {
		return "", fmt.Errorf("XDG_RUNTIME_DIR is not set")
	}
	base := filepath.Join(root, "scanocr-client")
	if err := os.MkdirAll(base, 0o700); err != nil {
		return "", fmt.Errorf("create runtime directory: %w", err)
	}
	if err := os.Chmod(base, 0o700); err != nil {
		return "", fmt.Errorf("protect runtime directory: %w", err)
	}
	return base, nil
}

func acquireCaptureLock(path string) (*captureLock, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("open capture lock: %w", err)
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
			return nil, errCaptureBusy
		}
		return nil, fmt.Errorf("lock capture: %w", err)
	}
	return &captureLock{file: file}, nil
}

func (lock *captureLock) Close() error {
	if lock == nil || lock.file == nil {
		return nil
	}
	err := syscall.Flock(int(lock.file.Fd()), syscall.LOCK_UN)
	closeErr := lock.file.Close()
	lock.file = nil
	if err != nil {
		return err
	}
	return closeErr
}

func newUUID() (string, error) {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", err
	}
	value[6] = value[6]&0x0f | 0x40
	value[8] = value[8]&0x3f | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		value[0:4], value[4:6], value[6:8], value[8:10], value[10:16]), nil
}

func stateBase() (string, error) {
	if root := os.Getenv("XDG_STATE_HOME"); root != "" {
		return filepath.Join(root, "scanocr"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".local", "state", "scanocr"), nil
}

func loadOrCreateClientID() (string, error) {
	base, err := stateBase()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(base, 0o700); err != nil {
		return "", err
	}
	if err := os.Chmod(base, 0o700); err != nil {
		return "", err
	}
	path := filepath.Join(base, "client-id")
	for {
		value, err := os.ReadFile(path)
		if err == nil {
			id := strings.TrimSpace(string(value))
			if id == "" {
				return "", fmt.Errorf("client-id is empty: %s", path)
			}
			return id, nil
		}
		if !errors.Is(err, os.ErrNotExist) {
			return "", err
		}
		id, err := newUUID()
		if err != nil {
			return "", err
		}
		file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if errors.Is(err, os.ErrExist) {
			continue
		}
		if err != nil {
			return "", err
		}
		if _, err = fmt.Fprintln(file, id); err != nil {
			_ = file.Close()
			_ = os.Remove(path)
			return "", err
		}
		if err := file.Close(); err != nil {
			_ = os.Remove(path)
			return "", err
		}
		return id, nil
	}
}
