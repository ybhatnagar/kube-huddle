package prometheus

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
)

// Two Hubble HTTP-duration series (seconds) — one team/api → team/worker at
// 0.005s and 0.007s, another team/api → team/db at 0.010s. Also one edge where
// destination_workload="unknown" that must be dropped, and one self-edge
// (team/api → team/api) that must also be dropped.
const hubbleLatencyResponse = `{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {
          "source_namespace":"team","source_workload":"api",
          "destination_namespace":"team","destination_workload":"worker"},
        "values": [[1600000000, "0.005"], [1600003600, "0.007"]]
      },
      {
        "metric": {
          "source_namespace":"team","source_workload":"api",
          "destination_namespace":"team","destination_workload":"db"},
        "values": [[1600000000, "0.010"]]
      },
      {
        "metric": {
          "source_namespace":"team","source_workload":"api",
          "destination_namespace":"team","destination_workload":"unknown"},
        "values": [[1600000000, "0.020"]]
      },
      {
        "metric": {
          "source_namespace":"team","source_workload":"api",
          "destination_namespace":"team","destination_workload":"api"},
        "values": [[1600000000, "0.030"]]
      }
    ]
  }
}`

func TestHubbleLatencySourceEmitsRawPairSamples(t *testing.T) {
	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.Query().Get("query")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(hubbleLatencyResponse))
	}))
	defer srv.Close()

	lc, err := connectors.Latency("hubble")
	if err != nil {
		t.Fatalf("hubble latency: %v", err)
	}
	cfg := connectors.Config{ClusterID: 3, Endpoint: srv.URL}
	win := connectors.Window{Start: time.Unix(1600000000, 0), End: time.Unix(1600007200, 0), Step: time.Hour}

	rows, err := lc.FetchLatencySamples(context.Background(), win, cfg)
	if err != nil {
		t.Fatalf("FetchLatencySamples: %v", err)
	}
	if !strings.Contains(gotQuery, "hubble_http_request_duration_seconds_sum") {
		t.Errorf("query didn't include Hubble duration metric: %q", gotQuery)
	}
	// 2 points api→worker + 1 point api→db = 3. Self-edges + destination=unknown dropped.
	if len(rows) != 3 {
		t.Fatalf("rows = %d, want 3", len(rows))
	}
	// All ms conversion (× 1000).
	first := rows[0]
	if first.SrcWorkloadUID != "team/Deployment/api" || first.DstWorkloadUID != "team/Deployment/worker" {
		t.Errorf("first pair = %q → %q", first.SrcWorkloadUID, first.DstWorkloadUID)
	}
	if first.LatencyMs != 5.0 {
		t.Errorf("first latency = %v ms, want 5.0", first.LatencyMs)
	}
	if first.Unit != "ms" {
		t.Errorf("unit = %q, want ms", first.Unit)
	}
	if first.ClusterID != 3 {
		t.Errorf("clusterID = %d, want 3", first.ClusterID)
	}
	if !first.TS.Equal(time.Unix(1600000000, 0).UTC()) {
		t.Errorf("ts = %v", first.TS)
	}
}
