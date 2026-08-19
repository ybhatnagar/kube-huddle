package ingest_test

// M2 end-to-end tests: the latency + discover Steps write the right rows into
// interaction_latency_samples, disc_pods (node_name), and disc_workloads (kind)
// against recorded k8s/Prometheus fixtures — i.e. the ingest orchestration wires
// the new connectors correctly.

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
	_ "github.com/kube-huddle/collector/internal/connectors/kubernetes" // register "kubernetes"
	kube "github.com/kube-huddle/collector/internal/connectors/kubernetes"
	_ "github.com/kube-huddle/collector/internal/connectors/prometheus" // register hubble latency
	"github.com/kube-huddle/collector/internal/ingest"
	"github.com/kube-huddle/collector/internal/store"
	_ "modernc.org/sqlite"
)

const hubbleLatency = `{"status":"success","data":{"resultType":"matrix","result":[
  {"metric":{"source_namespace":"team","source_workload":"api",
             "destination_namespace":"team","destination_workload":"worker"},
   "values":[[1600000000,"0.005"],[1600003600,"0.007"]]}
]}}`

const k8sNamespaces = `{"items":[{"metadata":{"name":"team"}},{"metadata":{"name":"kube-system"}}]}`
const k8sDeploys = `{"items":[
  {"metadata":{"name":"api","namespace":"team","labels":{"app":"api"}},
   "spec":{"replicas":2,"template":{"spec":{"containers":[]}}}},
  {"metadata":{"name":"worker","namespace":"team","labels":{"app":"worker"}},
   "spec":{"replicas":1,"template":{"spec":{"containers":[]}}}}
]}`
const k8sSts = `{"items":[]}`
const k8sDs = `{"items":[
  {"metadata":{"name":"log-fluent","namespace":"kube-system","labels":{"app":"log-fluent"}},
   "spec":{"template":{"spec":{"containers":[]}}}}
]}`
const k8sPods = `{"items":[
  {"metadata":{"name":"api-1","namespace":"team","labels":{"app":"api"},"ownerReferences":[{"kind":"ReplicaSet","name":"api-6f"}]},
   "spec":{"nodeName":"N1"}},
  {"metadata":{"name":"worker-1","namespace":"team","labels":{"app":"worker"},"ownerReferences":[{"kind":"ReplicaSet","name":"worker-a"}]},
   "spec":{"nodeName":"N2"}},
  {"metadata":{"name":"log-fluent-x","namespace":"kube-system","labels":{"app":"log-fluent"},"ownerReferences":[{"kind":"DaemonSet","name":"log-fluent"}]},
   "spec":{"nodeName":"N1"}}
]}`

// TestIngestLatencyAndDiscoverAgainstFixtures runs `ingest --latency` +
// `discover` against a recorded Prometheus + k8s API and asserts the state DB
// is populated correctly. This is the M2 "recorded fixture" end-to-end.
func TestIngestLatencyAndDiscoverAgainstFixtures(t *testing.T) {
	// One test server multiplexes both APIs to keep the test setup simple.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/v1/query_range"):
			_, _ = w.Write([]byte(hubbleLatency))
		case r.URL.Path == "/api/v1/namespaces":
			_, _ = w.Write([]byte(k8sNamespaces))
		case r.URL.Path == "/apis/apps/v1/deployments":
			_, _ = w.Write([]byte(k8sDeploys))
		case r.URL.Path == "/apis/apps/v1/statefulsets":
			_, _ = w.Write([]byte(k8sSts))
		case r.URL.Path == "/apis/apps/v1/daemonsets":
			_, _ = w.Write([]byte(k8sDs))
		case r.URL.Path == "/api/v1/pods":
			_, _ = w.Write([]byte(k8sPods))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	// The k8s connector uses a TLS transport by default; swap in the httptest
	// client so it talks plaintext HTTP to the mock server.
	if dc, err := connectors.Discover("kubernetes"); err == nil {
		if kc, ok := dc.(*kube.Connector); ok {
			kc.HTTP = srv.Client()
		}
	}

	path := filepath.Join(t.TempDir(), "m2.db")
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
		ClusterID:      cid,
		Source:         "prometheus",
		LatencySource:  "hubble",
		DiscoverSource: "kubernetes",
		Endpoint:       srv.URL,
		Window:         connectors.Window{Start: now.Add(-time.Hour), End: now, Step: time.Hour},
		Steps:          []string{"discover", "latency"},
	}
	run, err := ingest.Run(ctx, st, req)
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	if run.Status != store.StatusSuccess {
		t.Fatalf("status = %q (error=%q), want success", run.Status, run.Error)
	}
	_ = st.Close()

	// Reopen and verify contents.
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

	// disc_workloads: 3 rows (api, worker, log-fluent). Kind recorded.
	if got := count(`SELECT count(*) FROM disc_workloads`); got != 3 {
		t.Errorf("disc_workloads = %d, want 3", got)
	}
	var kinds []string
	rows, err := db.Query(`SELECT kind FROM disc_workloads ORDER BY name`)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var k string
		_ = rows.Scan(&k)
		kinds = append(kinds, k)
	}
	_ = rows.Close()
	if !contains(kinds, "DaemonSet") || !contains(kinds, "Deployment") {
		t.Errorf("kinds recorded = %v, want at least Deployment + DaemonSet", kinds)
	}

	// disc_pods: 3 pods, all with node_name.
	if got := count(`SELECT count(*) FROM disc_pods WHERE node_name IS NOT NULL AND node_name <> ''`); got != 3 {
		t.Errorf("disc_pods with node_name = %d, want 3", got)
	}

	// interaction_latency_samples: 2 points from the fixture (5ms + 7ms).
	if got := count(`SELECT count(*) FROM interaction_latency_samples`); got != 2 {
		t.Errorf("interaction_latency_samples = %d, want 2", got)
	}
	var last float64
	if err := db.QueryRow(
		`SELECT latency_ms FROM interaction_latency_samples ORDER BY ts DESC LIMIT 1`).Scan(&last); err != nil {
		t.Fatal(err)
	}
	if last != 7.0 {
		t.Errorf("latest latency_ms = %v, want 7.0", last)
	}
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}
