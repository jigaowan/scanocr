package main

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/BurntSushi/toml"
)

type Config struct {
	ServerURL  string
	Token      string
	ClientName string
	Notify     bool
}

type configFile struct {
	ServerURL  string `toml:"server_url"`
	Token      string `toml:"token"`
	ClientName string `toml:"client_name"`
	Notify     *bool  `toml:"notify"`
}

func defaultConfigPath() (string, error) {
	if root := os.Getenv("XDG_CONFIG_HOME"); root != "" {
		return filepath.Join(root, "scanocr", "client.toml"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".config", "scanocr", "client.toml"), nil
}

func loadConfig(path string) (Config, error) {
	var raw configFile
	metadata, err := toml.DecodeFile(path, &raw)
	if err != nil {
		return Config{}, fmt.Errorf("read config: %w", err)
	}
	if undecoded := metadata.Undecoded(); len(undecoded) != 0 {
		return Config{}, fmt.Errorf("unknown config key: %s", undecoded[0])
	}
	parsed, err := url.Parse(raw.ServerURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" {
		return Config{}, fmt.Errorf("server_url must be an absolute http or https URL")
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" || (parsed.Path != "" && parsed.Path != "/") {
		return Config{}, fmt.Errorf("server_url must not contain a path, query, or fragment")
	}
	if raw.Token == "" {
		return Config{}, fmt.Errorf("token must not be empty")
	}

	notify := true
	if raw.Notify != nil {
		notify = *raw.Notify
	}
	return Config{
		ServerURL:  strings.TrimRight(raw.ServerURL, "/"),
		Token:      raw.Token,
		ClientName: raw.ClientName,
		Notify:     notify,
	}, nil
}
