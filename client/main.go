package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

func usage() {
	fmt.Fprintf(os.Stderr, `Usage:
  scanocr-client --version
  scanocr-client [--config PATH] capture active|area
  scanocr-client [--config PATH] doctor
`)
}

func run(ctx context.Context, arguments []string) error {
	if len(arguments) == 1 && arguments[0] == "--version" {
		fmt.Println(clientVersion)
		return nil
	}
	configPath, err := defaultConfigPath()
	if err != nil {
		return err
	}
	if len(arguments) >= 2 && arguments[0] == "--config" {
		configPath = arguments[1]
		arguments = arguments[2:]
	}
	config, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	app := NewApp(config, os.Stdout, os.Stderr)
	if len(arguments) == 1 && arguments[0] == "doctor" {
		return app.Doctor(ctx)
	}
	if len(arguments) == 2 && arguments[0] == "capture" {
		err := app.Capture(ctx, arguments[1])
		if err != nil && err != errCaptureBusy {
			app.notify(context.Background(), "critical", err.Error())
		}
		return err
	}
	usage()
	return fmt.Errorf("invalid arguments")
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	defer stop()
	if err := run(ctx, os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "scanocr-client: %v\n", err)
		os.Exit(1)
	}
}
