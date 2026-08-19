// Package steps holds the ingestion Steps run by the collector's orchestrator.
// A Step = (select connector) -> (fetch window) -> (normalize) -> (upsert to
// store). Steps are registered like connectors so a new data type slots in. The
// orchestrator (in cmd/collector) resolves which Steps to run from CLI flags and
// runs them independently — a failing InteractionsStep must not kill MetricsStep.
package steps

import (
	"context"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/store"
)

// Runtime carries everything a Step needs for one run.
type Runtime struct {
	Store             store.StateStore
	Cfg               connectors.Config
	Window            connectors.Window
	Source            string // metrics connector name, e.g. "prometheus"
	InteractionSource string // interaction source name, e.g. "hubble" | "istio" | "otel"
	LatencySource     string // latency source name, e.g. "hubble" | "istio" | "otel"
	DiscoverSource    string // discover source name, e.g. "kubernetes"
}

// Result reports what a Step wrote.
type Result struct {
	RowsWritten int
	Workloads   int
}

// Step is one ingestion unit (metrics, interactions, …).
type Step interface {
	Name() string
	Run(ctx context.Context, rt Runtime) (Result, error)
}

var registry = map[string]Step{}

func register(s Step) { registry[s.Name()] = s }

// Get resolves a registered step by name.
func Get(name string) (Step, bool) {
	s, ok := registry[name]
	return s, ok
}

func init() {
	register(MetricsStep{})
	register(interactionsStep{})
	register(latencyStep{})
	register(discoverStep{})
}
