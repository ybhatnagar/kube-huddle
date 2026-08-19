"""Schema contract: verify the base tables exist after apply_schema."""


def test_apply_schema_creates_base_tables(store):
    """The schema should apply cleanly and create the core tables."""
    names = {r["name"] for r in store._fetchall(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"clusters", "data_sources", "settings", "disc_workloads",
            "metric_samples", "interactions", "collection_runs", "analysis_runs"} <= names


def test_analysis_run_roundtrip(store):
    """Verify we can create and read an analysis run."""
    cid = store.ensure_cluster("c1")
    run_id = store.create_analysis_run(
        name="test-run-1", cluster_id=cid, scope="all", config={},
        data_as_of=None, stale=True, ttl_hours=24,
    )
    run = store.get_run(run_id)
    assert run is not None
    assert run["name"] == "test-run-1"
    assert run["status"] == "running"
    assert run["run_type"] == "latency"
