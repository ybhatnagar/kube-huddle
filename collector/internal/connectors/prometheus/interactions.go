package prometheus

import (
	"context"
	"strings"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/store"
)

// interSource is a PromQL-backed InteractionConnector. The three registered sources
// (hubble | istio | otel) all read src->dst edge counts from Prometheus (the common
// denominator — Cilium/Hubble, Istio, and OTel/Tempo service-graph metrics are all
// commonly scraped there), differing only in the metric + label names. Everything is
// overridable via Config.Extra so a site can point it at its own metric/labels, and a
// native Hubble-Relay gRPC source can register under "hubble" later behind this same
// interface. docs/03 "Interaction-source selection".
type interSource struct {
	name  string
	query string // "$NS" is replaced with the namespace regex
	// label names carrying the edge endpoints
	srcNS, srcName, dstNS, dstName string
	c                              *Connector
}

func (s *interSource) Name() string { return s.name }

func (s *interSource) HealthCheck(ctx context.Context, cfg connectors.Config) error {
	return s.c.HealthCheck(ctx, cfg)
}

func (s *interSource) FetchInteractions(ctx context.Context, w connectors.Window, cfg connectors.Config) ([]store.Interaction, error) {
	q := s.query
	if v := cfg.Extra["interactions_query"]; v != "" {
		q = v
	}
	nsRe := ".+"
	if len(cfg.Namespaces) > 0 {
		nsRe = strings.Join(cfg.Namespaces, "|")
	}
	series, err := s.c.queryRange(ctx, cfg, strings.ReplaceAll(q, "$NS", nsRe), w)
	if err != nil {
		return nil, err
	}
	collectedAt := time.Now().UTC()
	labels := s.resolveLabels(cfg)

	var out []store.Interaction
	for _, ser := range series {
		src, ok1 := edgeUID(ser.Metric, labels.srcNS, labels.srcName)
		dst, ok2 := edgeUID(ser.Metric, labels.dstNS, labels.dstName)
		if !ok1 || !ok2 || src == dst || len(ser.Values) == 0 {
			continue
		}
		out = append(out, store.Interaction{
			ClusterID:      cfg.ClusterID,
			SrcWorkloadUID: src,
			DstWorkloadUID: dst,
			AvgCount:       ser.Values[len(ser.Values)-1].v, // last value in the window
			WindowStart:    w.Start,
			WindowEnd:      w.End,
			CollectedAt:    collectedAt,
		})
	}
	return out, nil
}

type labelSet struct{ srcNS, srcName, dstNS, dstName string }

func (s *interSource) resolveLabels(cfg connectors.Config) labelSet {
	pick := func(key, def string) string {
		if v := cfg.Extra[key]; v != "" {
			return v
		}
		return def
	}
	return labelSet{
		srcNS:   pick("label_src_ns", s.srcNS),
		srcName: pick("label_src_name", s.srcName),
		dstNS:   pick("label_dst_ns", s.dstNS),
		dstName: pick("label_dst_name", s.dstName),
	}
}

// edgeUID builds a workload_uid from an endpoint's namespace + name labels. Mesh/flow
// metrics rarely carry the workload kind, so kind defaults to Deployment (the common
// case) to stay consistent with the metrics uid scheme ns/Kind/name.
func edgeUID(m map[string]string, nsLabel, nameLabel string) (string, bool) {
	ns, name := m[nsLabel], m[nameLabel]
	if ns == "" || name == "" || name == "unknown" {
		return "", false
	}
	return ns + "/Deployment/" + name, true
}

func init() {
	c := New()
	// Istio telemetry: request counts between workloads.
	connectors.RegisterInteraction(&interSource{
		name:    "istio",
		query:   `sum by (source_workload_namespace, source_workload, destination_workload_namespace, destination_workload) (rate(istio_requests_total{reporter="destination"}[5m]))`,
		srcNS:   "source_workload_namespace", srcName: "source_workload",
		dstNS: "destination_workload_namespace", dstName: "destination_workload", c: c,
	})
	// Cilium/Hubble flow metrics (scraped by Prometheus).
	connectors.RegisterInteraction(&interSource{
		name:    "hubble",
		query:   `sum by (source_namespace, source_workload, destination_namespace, destination_workload) (rate(hubble_flows_processed_total{namespace=~"$NS"}[5m]))`,
		srcNS:   "source_namespace", srcName: "source_workload",
		dstNS: "destination_namespace", dstName: "destination_workload", c: c,
	})
	// OpenTelemetry / Tempo service-graph metrics.
	connectors.RegisterInteraction(&interSource{
		name:    "otel",
		query:   `sum by (client_namespace, client, server_namespace, server) (rate(traces_service_graph_request_total[5m]))`,
		srcNS:   "client_namespace", srcName: "client",
		dstNS: "server_namespace", dstName: "server", c: c,
	})
}
