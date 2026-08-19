package prometheus

import (
	"context"
	"strings"
	"time"

	"github.com/kube-huddle/collector/internal/connectors"
	"github.com/kube-huddle/collector/internal/store"
)

// latencySource is a PromQL-backed LatencyConnector. Same design as
// interSource: the three registered sources (hubble | otel | istio) all read
// src->dst pair latency from Prometheus (Hubble HTTP/DNS duration, OTel service
// graph, Istio request duration are all commonly scraped there), differing only
// in the metric + label names. Every query is overridable via Config.Extra.
//
// Emits raw ts/latency_ms samples — smoothing (α/i) happens in the engine so it
// stays re-tunable per run without re-collecting.
type latencySource struct {
	name string
	// query returns average per-pair latency at step-resolution. Convention:
	// the metric value MUST be in seconds; the connector converts to ms.
	query string
	// label names carrying the edge endpoints (same as interSource for consistency).
	srcNS, srcName, dstNS, dstName string
	c                              *Connector
}

func (s *latencySource) Name() string { return s.name }

func (s *latencySource) HealthCheck(ctx context.Context, cfg connectors.Config) error {
	return s.c.HealthCheck(ctx, cfg)
}

func (s *latencySource) FetchLatencySamples(ctx context.Context, w connectors.Window, cfg connectors.Config) ([]store.InteractionLatencySample, error) {
	q := s.query
	if v := cfg.Extra["latency_query"]; v != "" {
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

	var out []store.InteractionLatencySample
	for _, ser := range series {
		src, ok1 := edgeUID(ser.Metric, labels.srcNS, labels.srcName)
		dst, ok2 := edgeUID(ser.Metric, labels.dstNS, labels.dstName)
		if !ok1 || !ok2 || src == dst {
			continue
		}
		for _, pt := range ser.Values {
			// Prometheus returns latency in seconds by convention (Hubble/OTel
			// duration histograms); convert to ms for the samples table.
			out = append(out, store.InteractionLatencySample{
				ClusterID:      cfg.ClusterID,
				SrcWorkloadUID: src,
				DstWorkloadUID: dst,
				TS:             pt.t.UTC(),
				LatencyMs:      pt.v * 1000.0,
				Unit:           "ms",
				CollectedAt:    collectedAt,
			})
		}
	}
	return out, nil
}

func (s *latencySource) resolveLabels(cfg connectors.Config) labelSet {
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

func init() {
	c := New()
	// Cilium/Hubble HTTP request duration — avg per pair, in seconds. Override
	// via Config.Extra["latency_query"] to swap in DNS, TCP RTT, or a
	// site-specific histogram bucket derivative.
	connectors.RegisterLatency(&latencySource{
		name: "hubble",
		query: `sum by (source_namespace, source_workload, destination_namespace, destination_workload) (rate(hubble_http_request_duration_seconds_sum{namespace=~"$NS"}[5m]))` +
			` / sum by (source_namespace, source_workload, destination_namespace, destination_workload) (rate(hubble_http_request_duration_seconds_count{namespace=~"$NS"}[5m]))`,
		srcNS:   "source_namespace", srcName: "source_workload",
		dstNS: "destination_namespace", dstName: "destination_workload", c: c,
	})
	// Istio request duration (destination-reported): avg over 5m per pair, seconds.
	connectors.RegisterLatency(&latencySource{
		name: "istio",
		query: `sum by (source_workload_namespace, source_workload, destination_workload_namespace, destination_workload) (rate(istio_request_duration_milliseconds_sum{reporter="destination"}[5m]))` +
			` / sum by (source_workload_namespace, source_workload, destination_workload_namespace, destination_workload) (rate(istio_request_duration_milliseconds_count{reporter="destination"}[5m])) / 1000`,
		srcNS:   "source_workload_namespace", srcName: "source_workload",
		dstNS: "destination_workload_namespace", dstName: "destination_workload", c: c,
	})
	// OTel service-graph: avg client-observed latency, seconds.
	connectors.RegisterLatency(&latencySource{
		name: "otel",
		query: `sum by (client_namespace, client, server_namespace, server) (rate(traces_service_graph_request_client_seconds_sum[5m]))` +
			` / sum by (client_namespace, client, server_namespace, server) (rate(traces_service_graph_request_client_seconds_count[5m]))`,
		srcNS:   "client_namespace", srcName: "client",
		dstNS: "server_namespace", dstName: "server", c: c,
	})
}
