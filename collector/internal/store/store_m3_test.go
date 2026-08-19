package store

import (
	"context"
	"testing"
	"time"
)

func TestMetricSamplesAcceptFreeTextResource(t *testing.T) {
	// Post-0003, metric_samples.resource is free-text: a custom-resource utilization
	// series inserts without a CHECK violation.
	s := openTemp(t)
	ctx := context.Background()
	cid, _ := s.EnsureCluster(ctx, "default")
	ts := time.Unix(1600000000, 0).UTC()
	n, err := s.UpsertMetricSamples(ctx, []MetricSample{{
		ClusterID: cid, WorkloadUID: "ns/Deployment/ml", Resource: "nvidia.com/gpu",
		ResourceKind: KindCustom, TS: ts, Value: 0.8, Unit: "ratio", CollectedAt: ts,
	}})
	if err != nil || n != 1 {
		t.Fatalf("upsert custom-resource sample n=%d err=%v", n, err)
	}
	var kind string
	if err := s.db.QueryRow(
		`SELECT resource_kind FROM metric_samples WHERE resource = ?`, "nvidia.com/gpu").Scan(&kind); err != nil {
		t.Fatal(err)
	}
	if kind != KindCustom {
		t.Errorf("resource_kind = %q, want custom", kind)
	}
}

func TestUpsertInteractionsIsIdempotent(t *testing.T) {
	s := openTemp(t)
	ctx := context.Background()
	cid, _ := s.EnsureCluster(ctx, "default")
	ws := time.Unix(1600000000, 0).UTC()
	we := time.Unix(1600003600, 0).UTC()
	mk := func(c float64) []Interaction {
		return []Interaction{{
			ClusterID: cid, SrcWorkloadUID: "ns/Deployment/a", DstWorkloadUID: "ns/Deployment/b",
			AvgCount: c, WindowStart: ws, WindowEnd: we, CollectedAt: we,
		}}
	}
	if n, err := s.UpsertInteractions(ctx, mk(3)); err != nil || n != 1 {
		t.Fatalf("first upsert n=%d err=%v", n, err)
	}
	if _, err := s.UpsertInteractions(ctx, mk(7)); err != nil {
		t.Fatal(err)
	}
	var count int
	var avg float64
	if err := s.db.QueryRow(`SELECT count(*), avg_count FROM interactions`).Scan(&count, &avg); err != nil {
		t.Fatal(err)
	}
	if count != 1 || avg != 7 {
		t.Errorf("count=%d avg=%v, want 1 / 7 (updated on natural key)", count, avg)
	}
}

func TestResourceKindClassification(t *testing.T) {
	cases := map[string]string{
		"cpu": KindStandard, "memory": KindStandard, "ephemeral_storage": KindStandard,
		"net_tx": KindNetwork, "net_rx": KindNetwork,
		"nvidia.com/gpu": KindCustom, "example.com/hadoop-slots": KindCustom,
	}
	for res, want := range cases {
		if got := ResourceKindFor(res); got != want {
			t.Errorf("ResourceKindFor(%q) = %q, want %q", res, got, want)
		}
	}
	if !IsCustomResource("nvidia.com/gpu") || IsCustomResource("cpu") {
		t.Error("IsCustomResource classification wrong")
	}
}
