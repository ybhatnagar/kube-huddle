package store

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func openTemp(t *testing.T) *SQLStore {
	t.Helper()
	dsn := filepath.Join(t.TempDir(), "test.db")
	s, err := Open("sqlite", dsn)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	if err := s.Migrate(context.Background()); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return s
}

func TestMigrateCreatesAllTables(t *testing.T) {
	s := openTemp(t)
	want := []string{
		"schema_migrations", "clusters", "data_sources", "settings",
		"disc_namespaces", "disc_workloads", "disc_pods",
		"metric_samples", "interactions", "collection_runs",
		"analysis_runs", "recommendations", "recommendation_evidence", "recommendation_peers",
	}
	rows, err := s.db.Query(`SELECT name FROM sqlite_master WHERE type='table'`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	have := map[string]bool{}
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			t.Fatal(err)
		}
		have[n] = true
	}
	for _, tbl := range want {
		if !have[tbl] {
			t.Errorf("missing table %q", tbl)
		}
	}

	// The default settings row is seeded by the migration.
	var count int
	if err := s.db.QueryRow(`SELECT count(*) FROM settings`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Errorf("settings rows = %d, want 1", count)
	}
}

func TestMigrateIsIdempotent(t *testing.T) {
	s := openTemp(t)
	// Count the migration files bundled with the binary, so this test doesn't
	// need updating each time we add another migration — it only checks that
	// applying the set twice doesn't re-run any of them.
	entries, err := migrationsFS.ReadDir("migrations/sqlite")
	if err != nil {
		t.Fatal(err)
	}
	want := len(entries)

	if err := s.Migrate(context.Background()); err != nil {
		t.Fatalf("second migrate: %v", err)
	}
	var n int
	if err := s.db.QueryRow(`SELECT count(*) FROM schema_migrations`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != want {
		t.Errorf("schema_migrations rows = %d, want %d", n, want)
	}
}

func TestEnsureClusterIsStable(t *testing.T) {
	s := openTemp(t)
	ctx := context.Background()
	id1, err := s.EnsureCluster(ctx, "default")
	if err != nil {
		t.Fatal(err)
	}
	id2, err := s.EnsureCluster(ctx, "default")
	if err != nil {
		t.Fatal(err)
	}
	if id1 != id2 {
		t.Errorf("ensure returned %d then %d", id1, id2)
	}
}

func TestUpsertMetricSamplesIsIdempotent(t *testing.T) {
	s := openTemp(t)
	ctx := context.Background()
	cid, err := s.EnsureCluster(ctx, "default")
	if err != nil {
		t.Fatal(err)
	}
	ts := time.Unix(1600000000, 0).UTC()
	mk := func(v float64) []MetricSample {
		return []MetricSample{{
			ClusterID: cid, WorkloadUID: "ns/Deployment/app", Resource: ResourceCPU,
			TS: ts, Value: v, Unit: "cores", IsRate: true, CollectedAt: ts,
		}}
	}
	if n, err := s.UpsertMetricSamples(ctx, mk(0.5)); err != nil || n != 1 {
		t.Fatalf("first upsert n=%d err=%v", n, err)
	}
	if _, err := s.UpsertMetricSamples(ctx, mk(0.9)); err != nil {
		t.Fatal(err)
	}

	var count int
	var val float64
	if err := s.db.QueryRow(`SELECT count(*), max(value) FROM metric_samples`).Scan(&count, &val); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Errorf("rows = %d, want 1 (idempotent on natural key)", count)
	}
	if val != 0.9 {
		t.Errorf("value = %v, want 0.9 (updated in place)", val)
	}
}

func TestUpsertWorkloadsStoresIdentity(t *testing.T) {
	s := openTemp(t)
	ctx := context.Background()
	cid, err := s.EnsureCluster(ctx, "default")
	if err != nil {
		t.Fatal(err)
	}
	cpu := int64(250)
	if err := s.UpsertWorkloads(ctx, []WorkloadIdentity{{
		ClusterID: cid, WorkloadUID: "ns/Deployment/app", Namespace: "ns",
		Kind: "Deployment", Name: "app", RequestsCPUm: &cpu, FetchedAt: time.Now(),
	}}); err != nil {
		t.Fatal(err)
	}
	var name string
	var reqCPU int64
	if err := s.db.QueryRow(
		`SELECT name, requests_cpu_m FROM disc_workloads WHERE workload_uid = ?`,
		"ns/Deployment/app").Scan(&name, &reqCPU); err != nil {
		t.Fatal(err)
	}
	if name != "app" || reqCPU != 250 {
		t.Errorf("stored name=%q reqCPU=%d", name, reqCPU)
	}
}

func TestCollectionRunLifecycle(t *testing.T) {
	s := openTemp(t)
	ctx := context.Background()
	cid, err := s.EnsureCluster(ctx, "default")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	run := &CollectionRun{
		ClusterID: cid, Scope: `"all"`, Resources: `["cpu"]`,
		WindowStart: now.Add(-time.Hour), WindowEnd: now,
		Status: StatusRunning, StartedAt: &now,
	}
	id, err := s.CreateCollectionRun(ctx, run)
	if err != nil || id == 0 {
		t.Fatalf("create: id=%d err=%v", id, err)
	}
	run.Status = StatusSuccess
	run.RowsWritten = 42
	fin := time.Now().UTC()
	run.FinishedAt = &fin
	if err := s.UpdateCollectionRun(ctx, run); err != nil {
		t.Fatal(err)
	}
	var status string
	var rows int
	if err := s.db.QueryRow(`SELECT status, rows_written FROM collection_runs WHERE id = ?`, id).
		Scan(&status, &rows); err != nil {
		t.Fatal(err)
	}
	if status != StatusSuccess || rows != 42 {
		t.Errorf("status=%q rows=%d", status, rows)
	}
}
