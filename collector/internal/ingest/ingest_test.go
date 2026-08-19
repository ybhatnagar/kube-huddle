package ingest_test

import (
	"context"
	"database/sql"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	_ "github.com/kube-huddle/collector/internal/connectors/prometheus" // register connectors
	"github.com/kube-huddle/collector/internal/ingest"
	"github.com/kube-huddle/collector/internal/store"
	_ "modernc.org/sqlite"
)

const cpuMetrics = `{"status":"success","data":{"resultType":"matrix","result":[
  {"metric":{"namespace":"team","workload":"api","workload_type":"deployment"},"values":[[1600000000,"0.5"],[1600003600,"0.6"]]},
  {"metric":{"namespace":"team","workload":"worker","workload_type":"deployment"},"values":[[1600000000,"0.2"]]}
]}}`

const hubbleEdges = `{"status":"success","data":{"resultType":"matrix","result":[
  {"metric":{"source_namespace":"team","source_workload":"api","destination_namespace":"team","destination_workload":"worker"},"values":[[1600000000,"12"]]}
]}}`

// TestFullIngestPopulatesAllTiers runs the real metrics + interactions steps (via the
// registered Prometheus connector + hubble source) against a mock Prometheus, into a
// temp SQLite DB — the "recorded Prometheus fixture" e2e.
func TestFullIngestPopulatesAllTiers(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.Contains(q, "hubble_flows"):
			_, _ = w.Write([]byte(hubbleEdges))
		default:
			_, _ = w.Write([]byte(cpuMetrics))
		}
	}))
	defer srv.Close()

	path := filepath.Join(t.TempDir(), "e2e.db")
	st, err := store.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := st.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	cid, err := st.EnsureCluster(ctx, "default")
	if err != nil {
		t.Fatal(err)
	}

	now := time.Now().UTC()
	req := ingest.Request{
		ClusterID:         cid,
		Source:            "prometheus",
		InteractionSource: "hubble",
		Endpoint:          srv.URL,
		Resources:         []string{store.ResourceCPU},
		Window:            connectors.Window{Start: now.Add(-time.Hour), End: now, Step: time.Hour},
		Steps:             []string{"metrics", "interactions"},
	}
	run, err := ingest.Run(ctx, st, req)
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	if run.Status != store.StatusSuccess {
		t.Fatalf("status = %q (error=%q), want success", run.Status, run.Error)
	}
	_ = st.Close()

	// Re-open read-only and assert every tier-3 table was populated.
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	count := func(q string) int {
		var n int
		if err := db.QueryRow(q).Scan(&n); err != nil {
			t.Fatalf("%s: %v", q, err)
		}
		return n
	}
	if got := count(`SELECT count(*) FROM metric_samples`); got != 3 {
		t.Errorf("metric_samples = %d, want 3", got)
	}
	if got := count(`SELECT count(*) FROM interactions`); got != 1 {
		t.Errorf("interactions = %d, want 1", got)
	}
	if got := count(`SELECT count(*) FROM disc_workloads`); got != 2 {
		t.Errorf("disc_workloads = %d, want 2", got)
	}
}
