package steps

import (
	"context"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
)

// discoverStep queries the Kubernetes API (read-only) and populates
// disc_namespaces, disc_workloads (with kind — so DaemonSets can be excluded
// downstream), and disc_pods (with node_name — so the engine's weighing engine
// can build the node graph). The connector is selected by rt.DiscoverSource
// (default "kubernetes"). Recommend-only; the collector never writes to the
// cluster.
type discoverStep struct{}

func (discoverStep) Name() string { return "discover" }

func (discoverStep) Run(ctx context.Context, rt Runtime) (Result, error) {
	source := rt.DiscoverSource
	if source == "" {
		source = "kubernetes"
	}
	dc, err := connectors.Discover(source)
	if err != nil {
		return Result{}, err
	}
	snap, err := dc.Discover(ctx, rt.Cfg)
	if err != nil {
		return Result{}, err
	}
	now := time.Now().UTC()
	if err := rt.Store.UpsertNamespaces(ctx, rt.Cfg.ClusterID, snap.Namespaces, now); err != nil {
		return Result{}, err
	}
	if err := rt.Store.UpsertWorkloads(ctx, snap.Workloads); err != nil {
		return Result{}, err
	}
	nPods, err := rt.Store.UpsertPods(ctx, snap.Pods)
	if err != nil {
		return Result{}, err
	}
	return Result{RowsWritten: nPods, Workloads: len(snap.Workloads)}, nil
}
