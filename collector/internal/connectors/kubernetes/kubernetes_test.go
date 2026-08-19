package kubernetes

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/kube-huddle/collector/internal/connectors"
)

// Recorded k8s API fixtures — one deployment + one daemonset + two pods (one
// per workload) spread across N1 and N2. This is the minimum shape the discover
// step needs: it must record `Deployment` and `DaemonSet` kinds (so the engine
// can exclude DaemonSets) and each pod's `nodeName` (so the engine can build
// the node graph).
const namespacesJSON = `{"items":[
  {"metadata":{"name":"team"}},
  {"metadata":{"name":"kube-system"}}
]}`

const deploymentsJSON = `{"items":[
  {"metadata":{"name":"api","namespace":"team","labels":{"app":"api"}},
   "spec":{"replicas":2,"template":{"spec":{"containers":[
     {"resources":{"requests":{"cpu":"250m","memory":"64Mi"}}}
   ]}}}}
]}`

const statefulsetsJSON = `{"items":[]}`

const daemonsetsJSON = `{"items":[
  {"metadata":{"name":"log-fluent","namespace":"kube-system","labels":{"app":"log-fluent"}},
   "spec":{"template":{"spec":{"containers":[
     {"resources":{"requests":{"cpu":"100m","memory":"32Mi"}}}
   ]}}}}
]}`

// Pods: api-xxx-yyy on N1 (owned by ReplicaSet "api-xxx" — label carries the
// Deployment name); log-fluent-abc on N2 (owned by DaemonSet).
const podsJSON = `{"items":[
  {"metadata":{"name":"api-6f-7g","namespace":"team",
              "labels":{"app":"api"},
              "ownerReferences":[{"kind":"ReplicaSet","name":"api-6f"}]},
   "spec":{"nodeName":"N1"}},
  {"metadata":{"name":"log-fluent-abc","namespace":"kube-system",
              "labels":{"app":"log-fluent"},
              "ownerReferences":[{"kind":"DaemonSet","name":"log-fluent"}]},
   "spec":{"nodeName":"N2"}}
]}`

func TestKubernetesDiscoverPopulatesKindAndNodeName(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/api/v1/namespaces":
			_, _ = w.Write([]byte(namespacesJSON))
		case "/apis/apps/v1/deployments":
			_, _ = w.Write([]byte(deploymentsJSON))
		case "/apis/apps/v1/statefulsets":
			_, _ = w.Write([]byte(statefulsetsJSON))
		case "/apis/apps/v1/daemonsets":
			_, _ = w.Write([]byte(daemonsetsJSON))
		case "/api/v1/pods":
			_, _ = w.Write([]byte(podsJSON))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	dc, err := connectors.Discover("kubernetes")
	if err != nil {
		t.Fatalf("discover connector: %v", err)
	}
	// Point the HTTP client at the test server via cfg.Endpoint. The Connector's
	// default HTTP client uses a TLS-wrapped transport; override with a plain
	// client so httptest works.
	if c, ok := dc.(*Connector); ok {
		c.HTTP = srv.Client()
	}
	cfg := connectors.Config{ClusterID: 42, Endpoint: srv.URL}

	snap, err := dc.Discover(context.Background(), cfg)
	if err != nil {
		t.Fatalf("Discover: %v", err)
	}

	if len(snap.Namespaces) != 2 {
		t.Fatalf("namespaces = %d, want 2", len(snap.Namespaces))
	}

	// Two workloads: one Deployment, one DaemonSet. Both must carry `Kind`.
	if len(snap.Workloads) != 2 {
		t.Fatalf("workloads = %d, want 2", len(snap.Workloads))
	}
	byKind := map[string]int{}
	for _, w := range snap.Workloads {
		byKind[w.Kind]++
		if w.ClusterID != 42 {
			t.Errorf("workload %q clusterID = %d, want 42", w.Name, w.ClusterID)
		}
		if !strings.Contains(w.WorkloadUID, "/"+w.Kind+"/") {
			t.Errorf("workload_uid should embed kind: %q", w.WorkloadUID)
		}
	}
	if byKind["Deployment"] != 1 || byKind["DaemonSet"] != 1 {
		t.Errorf("wanted 1 Deployment + 1 DaemonSet, got %v", byKind)
	}

	// Two pods, both with nodeName populated (the whole point of discover).
	if len(snap.Pods) != 2 {
		t.Fatalf("pods = %d, want 2", len(snap.Pods))
	}
	byNode := map[string]string{}
	for _, p := range snap.Pods {
		if p.NodeName == "" {
			t.Errorf("pod %q missing NodeName", p.PodName)
		}
		if p.WorkloadName == "" {
			t.Errorf("pod %q missing WorkloadName", p.PodName)
		}
		byNode[p.NodeName] = p.WorkloadName
	}
	if byNode["N1"] != "api" {
		t.Errorf("N1 workload = %q, want api", byNode["N1"])
	}
	if byNode["N2"] != "log-fluent" {
		t.Errorf("N2 workload = %q, want log-fluent", byNode["N2"])
	}
}

func TestParseCPUMilli(t *testing.T) {
	cases := map[string]int64{"100m": 100, "1": 1000, "2.5": 2500, "250m": 250}
	for in, want := range cases {
		got, err := parseCPUMilli(in)
		if err != nil {
			t.Errorf("parseCPUMilli(%q): %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("parseCPUMilli(%q) = %d, want %d", in, got, want)
		}
	}
}

func TestParseMemBytes(t *testing.T) {
	cases := map[string]int64{"64Mi": 64 << 20, "1Gi": 1 << 30, "500M": 500_000_000, "1024": 1024}
	for in, want := range cases {
		got, err := parseMemBytes(in)
		if err != nil {
			t.Errorf("parseMemBytes(%q): %v", in, err)
			continue
		}
		if got != want {
			t.Errorf("parseMemBytes(%q) = %d, want %d", in, got, want)
		}
	}
}
