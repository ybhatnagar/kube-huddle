// Package config resolves collector settings with precedence flags > env > file >
// default. The optional --config file is JSON keyed by flag name so
// CronJob manifests can stay declarative while flags still override at runtime.
package config

import (
	"encoding/json"
	"flag"
	"os"
)

// Resolver merges explicitly-set flags, a JSON config file, and env vars.
type Resolver struct {
	set  map[string]string // flags the user explicitly passed
	file map[string]string // values from --config
}

// NewResolver captures which flags were explicitly set on fs (after fs.Parse) and
// loads the optional JSON config file at configPath (empty = none).
func NewResolver(fs *flag.FlagSet, configPath string) (*Resolver, error) {
	r := &Resolver{set: map[string]string{}, file: map[string]string{}}
	fs.Visit(func(f *flag.Flag) { r.set[f.Name] = f.Value.String() })

	if configPath != "" {
		b, err := os.ReadFile(configPath)
		if err != nil {
			return nil, err
		}
		if err := json.Unmarshal(b, &r.file); err != nil {
			return nil, err
		}
	}
	return r, nil
}

// String returns the value for flag `name`, honoring flags > env > file > def.
func (r *Resolver) String(name, envKey, def string) string {
	if v, ok := r.set[name]; ok {
		return v
	}
	if envKey != "" {
		if v := os.Getenv(envKey); v != "" {
			return v
		}
	}
	if v, ok := r.file[name]; ok && v != "" {
		return v
	}
	return def
}
