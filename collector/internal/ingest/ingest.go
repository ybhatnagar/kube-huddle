// Package ingest holds the collection orchestration shared by the CLI
// (`collector ingest`) and the trigger service (`collector serve` → POST /ingest):
// create a collection_runs row, run the selected steps, update the row.
package ingest

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/steps"
	"github.com/kube-huddle/collector/internal/store"
)

// Request describes one collection.
type Request struct {
	ClusterID         int64
	Source            string // metrics connector name, e.g. "prometheus"
	InteractionSource string // interaction source name, e.g. "hubble" | "istio" | "otel"
	LatencySource     string // latency source name, e.g. "hubble" | "istio" | "otel"
	DiscoverSource    string // discover source name, e.g. "kubernetes"
	Endpoint          string // metrics source URL
	Auth              connectors.AuthConfig
	Namespaces        []string // empty = all
	Resources         []string
	Window            connectors.Window
	Steps             []string // e.g. ["metrics","interactions","latency","discover"]
	Extra             map[string]string
}

// Start creates the collection_runs row (status running) and returns it. The
// service returns the id immediately, then finishes in the background.
func Start(ctx context.Context, st store.StateStore, req Request) (*store.CollectionRun, error) {
	now := time.Now().UTC()
	sources := []string{req.Source}
	if req.InteractionSource != "" {
		sources = append(sources, req.InteractionSource)
	}
	if req.LatencySource != "" && req.LatencySource != req.InteractionSource {
		sources = append(sources, req.LatencySource)
	}
	if req.DiscoverSource != "" {
		sources = append(sources, req.DiscoverSource)
	}
	run := &store.CollectionRun{
		ClusterID:   req.ClusterID,
		Scope:       scopeJSON(req.Namespaces),
		Resources:   marshalJSON(req.Resources),
		WindowStart: req.Window.Start,
		WindowEnd:   req.Window.End,
		SourcesUsed: marshalJSON(sources),
		Status:      store.StatusRunning,
		StartedAt:   &now,
	}
	if _, err := st.CreateCollectionRun(ctx, run); err != nil {
		return nil, err
	}
	return run, nil
}

// Finish runs the selected steps and updates the collection_runs row. Steps are
// independent — one failing doesn't abort the others; failures are recorded on the row.
func Finish(ctx context.Context, st store.StateStore, req Request, run *store.CollectionRun) error {
	cfg := connectors.Config{
		ClusterID: req.ClusterID, Endpoint: req.Endpoint,
		Namespaces: req.Namespaces, Resources: req.Resources, Auth: req.Auth, Extra: req.Extra,
	}
	var (
		totalRows int
		errs      []string
	)
	for _, name := range req.Steps {
		stp, ok := steps.Get(name)
		if !ok {
			errs = append(errs, name+": no such step")
			continue
		}
		res, err := stp.Run(ctx, steps.Runtime{
			Store: st, Cfg: cfg, Window: req.Window,
			Source: req.Source, InteractionSource: req.InteractionSource,
			LatencySource: req.LatencySource, DiscoverSource: req.DiscoverSource,
		})
		if err != nil {
			errs = append(errs, name+": "+err.Error())
			continue
		}
		totalRows += res.RowsWritten
	}
	finished := time.Now().UTC()
	run.Status = statusFor(len(errs), len(req.Steps), totalRows)
	run.RowsWritten = totalRows
	run.DataAsOf = &finished
	run.FinishedAt = &finished
	run.Error = strings.Join(errs, "; ")
	return st.UpdateCollectionRun(ctx, run)
}

// Run does Start + Finish synchronously (the CLI path).
func Run(ctx context.Context, st store.StateStore, req Request) (*store.CollectionRun, error) {
	run, err := Start(ctx, st, req)
	if err != nil {
		return nil, err
	}
	return run, Finish(ctx, st, req, run)
}

// ParseSince accepts durations like "7d", "24h", "90m".
func ParseSince(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if strings.HasSuffix(s, "d") {
		n, err := strconv.Atoi(strings.TrimSuffix(s, "d"))
		if err != nil {
			return 0, fmt.Errorf("invalid duration %q", s)
		}
		return time.Duration(n) * 24 * time.Hour, nil
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, fmt.Errorf("invalid duration %q", s)
	}
	return d, nil
}

func statusFor(nErr, nSteps, rows int) string {
	switch {
	case nErr == 0:
		return store.StatusSuccess
	case rows > 0 || nErr < nSteps:
		return store.StatusPartial
	default:
		return store.StatusFailed
	}
}

func scopeJSON(namespaces []string) string {
	if len(namespaces) == 0 {
		return `"all"`
	}
	return marshalJSON(map[string][]string{"namespaces": namespaces})
}

func marshalJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "null"
	}
	return string(b)
}
