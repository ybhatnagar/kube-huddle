package steps

import (
	"context"

	"github.com/kube-huddle/collector/internal/connectors"
)

// latencyStep pulls raw pair-latency samples via the selected LatencySource
// (hubble | otel | istio) and upserts them into interaction_latency_samples.
// Raw only — the engine does the α/i smoothing (docs/05 Stage 1).
type latencyStep struct{}

func (latencyStep) Name() string { return "latency" }

func (latencyStep) Run(ctx context.Context, rt Runtime) (Result, error) {
	source := rt.LatencySource
	if source == "" {
		source = "hubble" // documented default (docs/02)
	}
	lc, err := connectors.Latency(source)
	if err != nil {
		return Result{}, err
	}
	rows, err := lc.FetchLatencySamples(ctx, rt.Window, rt.Cfg)
	if err != nil {
		return Result{}, err
	}
	n, err := rt.Store.UpsertLatencySamples(ctx, rows)
	if err != nil {
		return Result{}, err
	}
	return Result{RowsWritten: n}, nil
}
