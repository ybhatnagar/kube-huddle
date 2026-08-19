package prometheus

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/store"
)

func TestFetchMetricsArbitraryResourceViaExtra(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(matrixResponse)) // reused: carries workload labels
	}))
	defer srv.Close()

	c := New()
	cfg := connectors.Config{
		ClusterID: 1, Endpoint: srv.URL,
		Resources: []string{"gpu_util"}, // not one of the canonical five
		Extra: map[string]string{
			"query_gpu_util": `avg by (namespace, workload, workload_type) (DCGM_FI_DEV_GPU_UTIL)`,
			"unit_gpu_util":  "percent",
			"rate_gpu_util":  "false",
		},
	}
	win := connectors.Window{Start: time.Unix(1600000000, 0), End: time.Unix(1600007200, 0), Step: time.Hour}

	res, err := c.FetchMetrics(context.Background(), win, cfg)
	if err != nil {
		t.Fatalf("FetchMetrics arbitrary: %v", err)
	}
	if len(res.Samples) == 0 {
		t.Fatal("no samples for arbitrary resource")
	}
	s0 := res.Samples[0]
	if s0.Resource != "gpu_util" || s0.ResourceKind != store.KindCustom {
		t.Errorf("resource=%q kind=%q, want gpu_util/custom", s0.Resource, s0.ResourceKind)
	}
	if s0.Unit != "percent" || s0.IsRate {
		t.Errorf("unit=%q isRate=%v, want percent/false", s0.Unit, s0.IsRate)
	}
}

func TestFetchMetricsUnknownResourceWithoutQueryErrors(t *testing.T) {
	c := New()
	cfg := connectors.Config{Endpoint: "http://unused", Resources: []string{"mystery"}}
	win := connectors.Window{Start: time.Unix(1, 0), End: time.Unix(2, 0), Step: time.Hour}
	if _, err := c.FetchMetrics(context.Background(), win, cfg); err == nil {
		t.Fatal("expected error for unknown resource without a query override")
	}
}

const istioEdges = `{"status":"success","data":{"resultType":"matrix","result":[
  {"metric":{"source_workload_namespace":"shop","source_workload":"frontend","destination_workload_namespace":"shop","destination_workload":"cart"},"values":[[1600000000,"3"],[1600003600,"5"]]},
  {"metric":{"source_workload_namespace":"shop","source_workload":"cart","destination_workload_namespace":"shop","destination_workload":"unknown"},"values":[[1600000000,"9"]]},
  {"metric":{"source_workload_namespace":"shop","source_workload":"self","destination_workload_namespace":"shop","destination_workload":"self"},"values":[[1600000000,"2"]]}
]}}`

func TestInteractionSourceParsesEdges(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(istioEdges))
	}))
	defer srv.Close()

	ic, err := connectors.Interaction("istio") // resolved from the registry
	if err != nil {
		t.Fatalf("resolve istio source: %v", err)
	}
	cfg := connectors.Config{ClusterID: 4, Endpoint: srv.URL}
	win := connectors.Window{Start: time.Unix(1600000000, 0), End: time.Unix(1600003600, 0), Step: time.Hour}

	edges, err := ic.FetchInteractions(context.Background(), win, cfg)
	if err != nil {
		t.Fatalf("FetchInteractions: %v", err)
	}
	// "unknown" destination skipped; self->self skipped; one real edge remains.
	if len(edges) != 1 {
		t.Fatalf("edges = %d, want 1", len(edges))
	}
	e := edges[0]
	if e.SrcWorkloadUID != "shop/Deployment/frontend" || e.DstWorkloadUID != "shop/Deployment/cart" {
		t.Errorf("edge uids wrong: %s -> %s", e.SrcWorkloadUID, e.DstWorkloadUID)
	}
	if e.AvgCount != 5 { // last value in the window
		t.Errorf("avg_count = %v, want 5", e.AvgCount)
	}
	if e.ClusterID != 4 {
		t.Errorf("clusterID = %d, want 4", e.ClusterID)
	}
}

func TestConnectorsRegistered(t *testing.T) {
	for _, name := range []string{"hubble", "istio", "otel"} {
		if _, err := connectors.Interaction(name); err != nil {
			t.Errorf("interaction source %q not registered: %v", name, err)
		}
	}
}
