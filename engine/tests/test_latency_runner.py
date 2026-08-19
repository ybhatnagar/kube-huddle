"""End-to-end tests for the latency head: fig2 cover, sparse migration, no-data
replicate. Uses the shared `store` fixture (fresh SQLite per test).
"""
from __future__ import annotations

import pytest

from engine.runner import run_analysis
from engine.synth import (
    fig2_cluster, no_data_cluster, seed_latency_cluster, sparse_migration_cluster,
)


def test_run_latency_analysis_on_fig2_writes_five_group_cover(store):
    fx = fig2_cluster()
    cluster_id = seed_latency_cluster(store, fx)
    assert cluster_id > 0

    res = run_analysis(store, cluster=cluster_id, run_type="latency")
    assert res.status == "completed"
    assert res.groups == 5
    assert res.recommendations >= 8   # one replicate rec per workload

    groups = store.list_latency_groups(res.run_id)
    partitions = [set(g["node_names"]) for g in groups]
    assert {"N1", "N3"} in partitions
    assert {"N2", "N4"} in partitions
    assert {"N5", "N6"} in partitions
    assert {"N7"} in partitions
    assert {"N8"} in partitions

    total = sum(float(g["latency_ratio"] or 0.0) for g in groups)
    assert total == pytest.approx(1 / 9 + 5 / 27 + 1 / 20)
    assert round(total, 2) == 0.35


def test_run_latency_dispatch_rejects_unknown_run_type(store):
    with pytest.raises(ValueError, match="unknown run_type"):
        run_analysis(store, cluster="c1", run_type="notarealtype")


def test_run_latency_sparse_fixture_emits_migrate_rec(store):
    """The sparse_migration_cluster fixture is engineered so app C in island
    {N5} has one dominant peer in island {N1,N2}. The whatif stage must emit a
    migrate rec for C, and it should be persisted with a copy-pasteable
    nodeAffinity snippet keyed to N1/N2."""
    fx = sparse_migration_cluster()
    cid = seed_latency_cluster(store, fx)

    def cost_of(uid):
        return 50.0 if uid.endswith("/C") else None

    res = run_analysis(
        store, cluster=cid, run_type="latency", cost_provider=cost_of,
    )
    assert res.status == "completed"

    recs = store.list_latency_recommendations(res.run_id)
    by_wl = {r["workload_uid"]: r for r in recs}
    c_uid = "default/Deployment/C"
    assert c_uid in by_wl
    c_rec = by_wl[c_uid]
    assert c_rec["action"] == "migrate"
    assert c_rec["suggested_mechanism"] == "node_affinity"
    assert c_rec["suggested_snippet"] is not None
    assert "nodeAffinity" in c_rec["suggested_snippet"]
    # Snippet should target A1's island (N1, N2), not B's (N3, N4) or itself (N5).
    assert "N1" in c_rec["suggested_snippet"]
    assert "N2" in c_rec["suggested_snippet"]
    assert "N3" not in c_rec["suggested_snippet"]
    assert "N5" not in c_rec["suggested_snippet"]


def test_run_latency_no_data_fixture_flags_lonely_node(store):
    fx = no_data_cluster()
    cid = seed_latency_cluster(store, fx)
    res = run_analysis(store, cluster=cid, run_type="latency")
    assert res.status == "completed"

    recs = store.list_latency_recommendations(res.run_id)
    by_wl = {r["workload_uid"]: r for r in recs}
    lonely = by_wl["default/Deployment/lonely"]
    assert lonely["action"] == "replicate"
    assert lonely["confidence"] == "low"
    assert "insufficient" in lonely["summary_text"].lower()


def test_run_latency_daemonset_workload_gets_no_recommendation(store):
    """Directive: DaemonSets already run on every node, so they're neither a
    replicate nor a migrate candidate — whatif skips them entirely."""
    # Seed the no_data fixture and then upsert `lonely` as a DaemonSet before
    # running.
    fx = no_data_cluster()
    cid = seed_latency_cluster(store, fx)
    store.upsert_workload(
        cluster_id=cid, workload_uid="default/Deployment/lonely",
        namespace="default", kind="DaemonSet", name="lonely",
    )
    res = run_analysis(store, cluster=cid, run_type="latency")
    recs = store.list_latency_recommendations(res.run_id)
    assert not any(r["workload_uid"] == "default/Deployment/lonely" for r in recs)
