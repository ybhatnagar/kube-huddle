// Package prometheus is the reference MetricsConnector: it pulls per-workload CPU
// and memory series from Prometheus via the HTTP API (/api/v1/query_range) and
// normalizes them into store.MetricSample records. It is the implementation other
// connectors copy. PromQL is fully configurable (Config.Extra) because the exact
// queries depend on the cluster's kube-state-metrics / recording-rule setup.
package prometheus

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/store"
)

func init() {
	c := New()
	connectors.RegisterMetrics(c)
}

// Connector implements connectors.MetricsConnector against Prometheus.
type Connector struct {
	// HTTP lets tests inject a client; nil uses a default with a sane timeout.
	HTTP *http.Client
}

// New returns a Prometheus connector with a default HTTP client.
func New() *Connector { return &Connector{HTTP: &http.Client{Timeout: 30 * time.Second}} }

func (c *Connector) Name() string { return "prometheus" }

// querySpec is the PromQL + normalization metadata for one resource. "$NS" is
// replaced with a namespace regex (all namespaces when none are selected).
type querySpec struct {
	promQL string
	unit   string
	isRate bool
}

// defaultQueries aggregate raw cAdvisor per-container/pod metrics up to the owning
// workload by joining on the kubernetes-mixin recording rule
// `namespace_workload_pod:kube_pod_owner:relabel` (shipped by kube-state-metrics /
// kube-prometheus-stack), which maps pod -> (workload, workload_type). Rates (CPU,
// network) are per-second; memory and storage are gauges. Override any of them per
// deployment via Config.Extra["query_<resource>"] to match a different metrics setup.
func defaultQueries() map[string]querySpec {
	join := func(inner string) string {
		return `sum by (namespace, workload, workload_type) (` + inner +
			` * on (namespace, pod) group_left (workload, workload_type) ` +
			`namespace_workload_pod:kube_pod_owner:relabel{namespace=~"$NS"})`
	}
	return map[string]querySpec{
		store.ResourceCPU: {
			promQL: join(`rate(container_cpu_usage_seconds_total{namespace=~"$NS", container!="", container!="POD"}[5m])`),
			unit:   "cores", isRate: true,
		},
		store.ResourceMemory: {
			promQL: join(`container_memory_working_set_bytes{namespace=~"$NS", container!="", container!="POD"}`),
			unit:   "bytes", isRate: false,
		},
		store.ResourceNetTx: {
			promQL: join(`rate(container_network_transmit_bytes_total{namespace=~"$NS"}[5m])`),
			unit:   "bytes/s", isRate: true,
		},
		store.ResourceNetRx: {
			promQL: join(`rate(container_network_receive_bytes_total{namespace=~"$NS"}[5m])`),
			unit:   "bytes/s", isRate: true,
		},
		store.ResourceEphemeralStorage: {
			promQL: join(`container_fs_usage_bytes{namespace=~"$NS", container!="", container!="POD"}`),
			unit:   "bytes", isRate: false,
		},
	}
}

func (c *Connector) resolveQueries(cfg connectors.Config) map[string]querySpec {
	q := defaultQueries()
	// Any resource's PromQL can be overridden via Config.Extra["query_<resource>"].
	for res, spec := range q {
		if v := cfg.Extra["query_"+res]; v != "" {
			spec.promQL = v
			q[res] = spec
		}
	}
	return q
}

// HealthCheck probes the Prometheus HTTP API.
func (c *Connector) HealthCheck(ctx context.Context, cfg connectors.Config) error {
	_, err := c.queryRange(ctx, cfg, "vector(1)", connectors.Window{
		Start: time.Now().Add(-1 * time.Minute), End: time.Now(), Step: time.Minute,
	})
	return err
}

// FetchMetrics pulls each requested resource and normalizes the matrix response.
func (c *Connector) FetchMetrics(ctx context.Context, w connectors.Window, cfg connectors.Config) (connectors.MetricsResult, error) {
	specs := c.resolveQueries(cfg)
	nsRe := ".+"
	if len(cfg.Namespaces) > 0 {
		nsRe = strings.Join(cfg.Namespaces, "|")
	}

	resources := cfg.Resources
	if len(resources) == 0 {
		resources = []string{store.ResourceCPU, store.ResourceMemory}
	}

	collectedAt := time.Now().UTC()
	var out connectors.MetricsResult
	seen := map[string]struct{}{}

	for _, res := range resources {
		spec, ok := specFor(res, specs, cfg)
		if !ok {
			return connectors.MetricsResult{}, fmt.Errorf(
				"no PromQL configured for resource %q (set Extra[\"query_%s\"])", res, res)
		}
		query := strings.ReplaceAll(spec.promQL, "$NS", nsRe)
		series, err := c.queryRange(ctx, cfg, query, w)
		if err != nil {
			return connectors.MetricsResult{}, fmt.Errorf("query %s: %w", res, err)
		}
		for _, s := range series {
			uid, ns, kind, name, ok := workloadFromLabels(s.Metric)
			if !ok {
				continue // series we can't attribute to a workload are skipped
			}
			if _, dup := seen[uid]; !dup {
				seen[uid] = struct{}{}
				out.Workloads = append(out.Workloads, store.WorkloadIdentity{
					ClusterID:   cfg.ClusterID,
					WorkloadUID: uid,
					Namespace:   ns,
					Kind:        kind,
					Name:        name,
					FetchedAt:   collectedAt,
				})
			}
			for _, p := range s.Values {
				out.Samples = append(out.Samples, store.MetricSample{
					ClusterID:    cfg.ClusterID,
					WorkloadUID:  uid,
					Resource:     res,
					ResourceKind: store.ResourceKindFor(res),
					TS:           p.t,
					Value:        p.v,
					Unit:         spec.unit,
					IsRate:       spec.isRate,
					CollectedAt:  collectedAt,
				})
			}
		}
	}
	return out, nil
}

// specFor resolves a resource's query: a built-in spec, else a fully custom one from
// Config.Extra["query_<res>"] (+ optional "unit_<res>" / "rate_<res>"). This is what
// lets utilization span *arbitrary* resources (docs/03), not just the canonical five.
func specFor(res string, specs map[string]querySpec, cfg connectors.Config) (querySpec, bool) {
	if s, ok := specs[res]; ok {
		return s, true
	}
	if q := cfg.Extra["query_"+res]; q != "" {
		return querySpec{promQL: q, unit: cfg.Extra["unit_"+res], isRate: cfg.Extra["rate_"+res] == "true"}, true
	}
	return querySpec{}, false
}

// --- HTTP + parsing --------------------------------------------------------

type point struct {
	t time.Time
	v float64
}

type series struct {
	Metric map[string]string
	Values []point
}

type promResponse struct {
	Status string `json:"status"`
	Data   struct {
		ResultType string `json:"resultType"`
		Result     []struct {
			Metric map[string]string    `json:"metric"`
			Values [][2]json.RawMessage `json:"values"`
		} `json:"result"`
	} `json:"data"`
	ErrorType string `json:"errorType"`
	Error     string `json:"error"`
}

func (c *Connector) queryRange(ctx context.Context, cfg connectors.Config, query string, w connectors.Window) ([]series, error) {
	base := strings.TrimRight(cfg.Endpoint, "/")
	if base == "" {
		return nil, fmt.Errorf("prometheus endpoint not configured")
	}
	step := w.Step
	if step <= 0 {
		step = time.Hour
	}
	form := url.Values{}
	form.Set("query", query)
	form.Set("start", strconv.FormatInt(w.Start.UTC().Unix(), 10))
	form.Set("end", strconv.FormatInt(w.End.UTC().Unix(), 10))
	form.Set("step", strconv.FormatFloat(step.Seconds(), 'f', -1, 64))

	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		base+"/api/v1/query_range?"+form.Encode(), nil)
	if err != nil {
		return nil, err
	}
	if cfg.Auth.Bearer != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.Auth.Bearer)
	} else if cfg.Auth.Username != "" {
		req.SetBasicAuth(cfg.Auth.Username, cfg.Auth.Password)
	}

	client := c.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("prometheus HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var pr promResponse
	if err := json.Unmarshal(body, &pr); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	if pr.Status != "success" {
		return nil, fmt.Errorf("prometheus error %s: %s", pr.ErrorType, pr.Error)
	}
	if pr.Data.ResultType != "matrix" {
		return nil, fmt.Errorf("expected matrix result, got %q", pr.Data.ResultType)
	}

	out := make([]series, 0, len(pr.Data.Result))
	for _, r := range pr.Data.Result {
		s := series{Metric: r.Metric}
		for _, pair := range r.Values {
			t, v, ok := parseSample(pair)
			if !ok {
				continue
			}
			s.Values = append(s.Values, point{t: t, v: v})
		}
		out = append(out, s)
	}
	return out, nil
}

// parseSample decodes a Prometheus [<unix ts number>, "<value string>"] pair.
func parseSample(pair [2]json.RawMessage) (time.Time, float64, bool) {
	var tsFloat float64
	if err := json.Unmarshal(pair[0], &tsFloat); err != nil {
		return time.Time{}, 0, false
	}
	var valStr string
	if err := json.Unmarshal(pair[1], &valStr); err != nil {
		return time.Time{}, 0, false
	}
	v, err := strconv.ParseFloat(valStr, 64)
	if err != nil || math.IsNaN(v) {
		return time.Time{}, 0, false
	}
	sec := int64(tsFloat)
	nsec := int64((tsFloat - float64(sec)) * 1e9)
	return time.Unix(sec, nsec).UTC(), v, true
}

// workloadFromLabels derives (uid, namespace, kind, name) from series labels. The
// collector's uid scheme is "namespace/kind/name" — stable, readable, and the join
// key the engine reads back from disc_workloads.
func workloadFromLabels(m map[string]string) (uid, ns, kind, name string, ok bool) {
	ns = firstNonEmpty(m["namespace"], m["ns"])
	name = firstNonEmpty(m["workload"], m["owner_name"], m["deployment"], m["pod"])
	kind = firstNonEmpty(m["workload_type"], m["owner_kind"])
	if kind == "" {
		if m["workload"] != "" {
			kind = "Deployment"
		} else if m["pod"] != "" {
			kind = "Pod"
		} else {
			kind = "Deployment"
		}
	}
	kind = normalizeKind(kind)
	if ns == "" || name == "" {
		return "", "", "", "", false
	}
	return ns + "/" + kind + "/" + name, ns, kind, name, true
}

func normalizeKind(k string) string {
	switch strings.ToLower(k) {
	case "deployment":
		return "Deployment"
	case "statefulset":
		return "StatefulSet"
	case "daemonset":
		return "DaemonSet"
	case "replicaset":
		return "ReplicaSet"
	case "job":
		return "Job"
	case "cronjob":
		return "CronJob"
	case "pod":
		return "Pod"
	default:
		if k == "" {
			return "Deployment"
		}
		return strings.ToUpper(k[:1]) + k[1:]
	}
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
