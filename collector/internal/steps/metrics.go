package steps

import (
	"context"

	"github.com/kube-huddle/collector/internal/connectors"
)

// MetricsStep pulls metric samples via the selected MetricsConnector and upserts
// them (plus the workload identities) to the state store.
type MetricsStep struct{}

func (MetricsStep) Name() string { return "metrics" }

func (MetricsStep) Run(ctx context.Context, rt Runtime) (Result, error) {
	mc, err := connectors.Metrics(rt.Source)
	if err != nil {
		return Result{}, err
	}
	res, err := mc.FetchMetrics(ctx, rt.Window, rt.Cfg)
	if err != nil {
		return Result{}, err
	}
	if err := rt.Store.UpsertWorkloads(ctx, res.Workloads); err != nil {
		return Result{}, err
	}
	n, err := rt.Store.UpsertMetricSamples(ctx, res.Samples)
	if err != nil {
		return Result{}, err
	}
	return Result{RowsWritten: n, Workloads: len(res.Workloads)}, nil
}

// interactionsStep pulls dependency edges via the selected InteractionSource
// (hubble | istio | otel) and upserts them to the interactions table.
type interactionsStep struct{}

func (interactionsStep) Name() string { return "interactions" }

func (interactionsStep) Run(ctx context.Context, rt Runtime) (Result, error) {
	source := rt.InteractionSource
	if source == "" {
		source = "hubble" // documented default (docs/03)
	}
	ic, err := connectors.Interaction(source)
	if err != nil {
		return Result{}, err
	}
	edges, err := ic.FetchInteractions(ctx, rt.Window, rt.Cfg)
	if err != nil {
		return Result{}, err
	}
	n, err := rt.Store.UpsertInteractions(ctx, edges)
	if err != nil {
		return Result{}, err
	}
	return Result{RowsWritten: n}, nil
}
