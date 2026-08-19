// Package connectors defines the ingestion extension point. A Connector is one
// (source × data-type); users add a source by implementing an interface and
// self-registering into the registry (plugin pattern), so `--source prometheus`
// resolves to the right implementation with no core changes. Every connector
// emits normalized store records, so the store/engine never learn source formats.
package connectors

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/kube-huddle/collector/internal/store"
)

// Window is the [Start, End] range to pull at Step resolution.
type Window struct {
	Start time.Time
	End   time.Time
	Step  time.Duration
}

// AuthConfig carries out-of-cluster auth for a connector (all optional; in-cluster
// defaults need none). TLS material is added in a later milestone.
type AuthConfig struct {
	Bearer   string
	Username string
	Password string
}

// Config is per-run connector configuration. Endpoint + auth are always
// config-driven (never hardcoded) so the same binary works in- and out-of-cluster.
type Config struct {
	ClusterID  int64
	Endpoint   string
	Namespaces []string // empty = all namespaces
	Resources  []string // e.g. ["cpu","memory"]
	Auth       AuthConfig
	Extra      map[string]string // connector-specific overrides (e.g. PromQL templates)
}

// MetricsResult is what a MetricsConnector returns: normalized samples plus the
// workload identities it observed (so the metrics Step can populate disc_workloads,
// which the engine joins on workload_uid for card labels + cost).
type MetricsResult struct {
	Samples   []store.MetricSample
	Workloads []store.WorkloadIdentity
}

// MetricsConnector pulls normalized metric samples from a source.
type MetricsConnector interface {
	Name() string
	HealthCheck(ctx context.Context, cfg Config) error
	FetchMetrics(ctx context.Context, w Window, cfg Config) (MetricsResult, error)
}

// InteractionConnector pulls dependency-graph edges from an interaction source
// (hubble | istio | otel), registered by name behind this one interface.
type InteractionConnector interface {
	Name() string
	HealthCheck(ctx context.Context, cfg Config) error
	FetchInteractions(ctx context.Context, w Window, cfg Config) ([]store.Interaction, error)
}

// LatencyConnector pulls per-pair raw latency samples from a source (hubble |
// otel | istio). The engine does the α/i smoothing (docs/05 Stage 1), so the
// collector only emits raw ts/latency_ms points.
type LatencyConnector interface {
	Name() string
	HealthCheck(ctx context.Context, cfg Config) error
	FetchLatencySamples(ctx context.Context, w Window, cfg Config) ([]store.InteractionLatencySample, error)
}

// DiscoverConnector reads Kubernetes discovery data via the k8s API — namespaces,
// workloads (with kind, so downstream can exclude DaemonSets), and pods (with
// nodeName, so the engine can place workloads on nodes). Read-only.
type DiscoverConnector interface {
	Name() string
	HealthCheck(ctx context.Context, cfg Config) error
	Discover(ctx context.Context, cfg Config) (DiscoverResult, error)
}

// DiscoverResult is the normalized snapshot a DiscoverConnector returns.
type DiscoverResult struct {
	Namespaces []string
	Workloads  []store.WorkloadIdentity
	Pods       []store.Pod
}

// --- registry --------------------------------------------------------------

var (
	regMu      sync.RWMutex
	metricsReg = map[string]MetricsConnector{}
	interReg   = map[string]InteractionConnector{}
	latencyReg = map[string]LatencyConnector{}
	discReg    = map[string]DiscoverConnector{}
)

// RegisterMetrics adds a metrics connector, keyed by Name(). Called from a
// connector package's init(); duplicate names panic (a programming error).
func RegisterMetrics(c MetricsConnector) {
	regMu.Lock()
	defer regMu.Unlock()
	if _, dup := metricsReg[c.Name()]; dup {
		panic("connectors: duplicate metrics connector " + c.Name())
	}
	metricsReg[c.Name()] = c
}

// RegisterInteraction adds an interaction connector, keyed by Name().
func RegisterInteraction(c InteractionConnector) {
	regMu.Lock()
	defer regMu.Unlock()
	if _, dup := interReg[c.Name()]; dup {
		panic("connectors: duplicate interaction connector " + c.Name())
	}
	interReg[c.Name()] = c
}

// RegisterLatency adds a latency connector, keyed by Name().
func RegisterLatency(c LatencyConnector) {
	regMu.Lock()
	defer regMu.Unlock()
	if _, dup := latencyReg[c.Name()]; dup {
		panic("connectors: duplicate latency connector " + c.Name())
	}
	latencyReg[c.Name()] = c
}

// RegisterDiscover adds a discover connector, keyed by Name().
func RegisterDiscover(c DiscoverConnector) {
	regMu.Lock()
	defer regMu.Unlock()
	if _, dup := discReg[c.Name()]; dup {
		panic("connectors: duplicate discover connector " + c.Name())
	}
	discReg[c.Name()] = c
}

// Metrics resolves a registered metrics connector by name.
func Metrics(name string) (MetricsConnector, error) {
	regMu.RLock()
	defer regMu.RUnlock()
	c, ok := metricsReg[name]
	if !ok {
		return nil, fmt.Errorf("no metrics connector registered for %q", name)
	}
	return c, nil
}

// Interaction resolves a registered interaction connector by name.
func Interaction(name string) (InteractionConnector, error) {
	regMu.RLock()
	defer regMu.RUnlock()
	c, ok := interReg[name]
	if !ok {
		return nil, fmt.Errorf("no interaction connector registered for %q", name)
	}
	return c, nil
}

// Latency resolves a registered latency connector by name.
func Latency(name string) (LatencyConnector, error) {
	regMu.RLock()
	defer regMu.RUnlock()
	c, ok := latencyReg[name]
	if !ok {
		return nil, fmt.Errorf("no latency connector registered for %q", name)
	}
	return c, nil
}

// Discover resolves a registered discover connector by name.
func Discover(name string) (DiscoverConnector, error) {
	regMu.RLock()
	defer regMu.RUnlock()
	c, ok := discReg[name]
	if !ok {
		return nil, fmt.Errorf("no discover connector registered for %q", name)
	}
	return c, nil
}

// Names lists registered connectors for `collector connectors list`.
func Names() (metrics, interactions, latency, discover []string) {
	regMu.RLock()
	defer regMu.RUnlock()
	for n := range metricsReg {
		metrics = append(metrics, n)
	}
	for n := range interReg {
		interactions = append(interactions, n)
	}
	for n := range latencyReg {
		latency = append(latency, n)
	}
	for n := range discReg {
		discover = append(discover, n)
	}
	sort.Strings(metrics)
	sort.Strings(interactions)
	sort.Strings(latency)
	sort.Strings(discover)
	return metrics, interactions, latency, discover
}
