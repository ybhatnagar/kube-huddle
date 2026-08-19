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

const matrixResponse = `{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {"namespace": "vmw-costing", "workload": "vmw-costing1", "workload_type": "deployment"},
        "values": [[1600000000, "0.5"], [1600003600, "2.5"], [1600007200, "NaN"]]
      },
      {
        "metric": {"namespace": "vmw-costing", "pod": "orphan-abc"},
        "values": [[1600000000, "0.1"]]
      }
    ]
  }
}`

func TestFetchMetricsParsesMatrix(t *testing.T) {
	var gotPath, gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.Query().Get("query")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(matrixResponse))
	}))
	defer srv.Close()

	c := New()
	cfg := connectors.Config{
		ClusterID: 7,
		Endpoint:  srv.URL,
		Resources: []string{store.ResourceCPU},
	}
	win := connectors.Window{Start: time.Unix(1600000000, 0), End: time.Unix(1600007200, 0), Step: time.Hour}

	res, err := c.FetchMetrics(context.Background(), win, cfg)
	if err != nil {
		t.Fatalf("FetchMetrics: %v", err)
	}

	if gotPath != "/api/v1/query_range" {
		t.Errorf("path = %q, want /api/v1/query_range", gotPath)
	}
	if gotQuery == "" {
		t.Errorf("query param was empty")
	}

	// Two valid points from series 1 (the NaN is dropped) + one from series 2.
	if len(res.Samples) != 3 {
		t.Fatalf("samples = %d, want 3", len(res.Samples))
	}
	// Two distinct workloads: the deployment and the pod-only (orphan) series.
	if len(res.Workloads) != 2 {
		t.Fatalf("workloads = %d, want 2", len(res.Workloads))
	}

	s0 := res.Samples[0]
	if s0.WorkloadUID != "vmw-costing/Deployment/vmw-costing1" {
		t.Errorf("uid = %q", s0.WorkloadUID)
	}
	if s0.Resource != store.ResourceCPU || !s0.IsRate || s0.Unit != "cores" {
		t.Errorf("normalization wrong: resource=%s isRate=%v unit=%s", s0.Resource, s0.IsRate, s0.Unit)
	}
	if s0.Value != 0.5 {
		t.Errorf("value = %v, want 0.5", s0.Value)
	}
	if !s0.TS.Equal(time.Unix(1600000000, 0).UTC()) {
		t.Errorf("ts = %v", s0.TS)
	}
	if s0.ClusterID != 7 {
		t.Errorf("clusterID = %d, want 7", s0.ClusterID)
	}
}

func TestFetchMetricsSurfacesPromError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"status":"error","errorType":"bad_data","error":"parse error"}`))
	}))
	defer srv.Close()

	c := New()
	cfg := connectors.Config{Endpoint: srv.URL, Resources: []string{store.ResourceCPU}}
	win := connectors.Window{Start: time.Unix(1, 0), End: time.Unix(2, 0), Step: time.Hour}
	if _, err := c.FetchMetrics(context.Background(), win, cfg); err == nil {
		t.Fatal("expected error on HTTP 400, got nil")
	}
}
