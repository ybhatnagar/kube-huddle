"""Unit tests for whatif — replicate vs migrate emission (docs/05 §B.4)."""
from __future__ import annotations

import networkx as nx
import pytest

from engine.recommenders.latency.whatif import WhatifConfig, whatif


def _cfg(**kw):
    return WhatifConfig(**{**dict(enable_migration=True, cost_enabled=True), **kw})


@pytest.fixture
def three_islands_graph():
    """Match the sparse_migration_cluster fixture's node graph.

    Islands: {N1,N2}, {N3,N4}, {N5}. Inter-island edges have higher weight so
    the group cover the runner would produce is the three islands as above.
    """
    g = nx.Graph()
    # Intra-island edges.
    g.add_edge("N1", "N2", weight_ms=1.0, count=1)
    g.add_edge("N3", "N4", weight_ms=1.0, count=1)
    # Inter-island edges (high latency so islands stay separate).
    g.add_edge("N1", "N3", weight_ms=20.0, count=1)
    g.add_edge("N2", "N4", weight_ms=20.0, count=1)
    g.add_edge("N5", "N1", weight_ms=15.0, count=1)
    g.add_edge("N5", "N3", weight_ms=25.0, count=1)
    return g


@pytest.fixture
def sparse_setup(three_islands_graph):
    cover = [
        frozenset({"N1", "N2"}),
        frozenset({"N3", "N4"}),
        frozenset({"N5"}),
    ]
    workloads = [
        {"workload_uid": "d/Deployment/A1", "name": "A1", "kind": "Deployment", "namespace": "d"},
        {"workload_uid": "d/Deployment/A2", "name": "A2", "kind": "Deployment", "namespace": "d"},
        {"workload_uid": "d/Deployment/B1", "name": "B1", "kind": "Deployment", "namespace": "d"},
        {"workload_uid": "d/Deployment/B2", "name": "B2", "kind": "Deployment", "namespace": "d"},
        {"workload_uid": "d/Deployment/C",  "name": "C",  "kind": "Deployment", "namespace": "d"},
    ]
    placement = {
        "d/Deployment/A1": "N1", "d/Deployment/A2": "N2",
        "d/Deployment/B1": "N3", "d/Deployment/B2": "N4",
        "d/Deployment/C":  "N5",
    }
    # Intra-island pairs anchor A1/A2/B1/B2 to their home groups so migration
    # isn't triggered on them. The two C↔peer pairs give C its one lopsided
    # dominant peer (A1) plus a tiny cross-island tail (B1).
    pair_vol = {
        ("d/Deployment/A1", "d/Deployment/A2"): 500.0,
        ("d/Deployment/B1", "d/Deployment/B2"): 500.0,
        ("d/Deployment/C", "d/Deployment/A1"): 100.0,
        ("d/Deployment/C", "d/Deployment/B1"): 2.0,
    }
    pair_lat = {
        ("d/Deployment/A1", "d/Deployment/A2"): 1.0,
        ("d/Deployment/B1", "d/Deployment/B2"): 1.0,
        ("d/Deployment/C", "d/Deployment/A1"): 15.0,
        ("d/Deployment/C", "d/Deployment/B1"): 25.0,
    }
    return three_islands_graph, cover, workloads, placement, pair_vol, pair_lat


def test_whatif_emits_migrate_for_low_crossgroup_workload(sparse_setup):
    g, cover, wls, placement, vol, lat = sparse_setup
    recs = whatif(g, cover, wls, placement, vol, lat, no_data_nodes=(), cfg=_cfg())
    by_uid = {r.workload_uid: r for r in recs}
    c_rec = by_uid["d/Deployment/C"]
    assert c_rec.action == "migrate"
    # Dominant peer A1 is in group 0 → C should migrate into group 0.
    assert c_rec.to_group_index == 0
    # Suggested snippet targets group 0's nodes (N1, N2).
    assert "N1" in c_rec.suggested_snippet
    assert "N2" in c_rec.suggested_snippet
    # Latency delta must be non-positive (migration should not worsen).
    assert c_rec.latency_delta_ms is not None
    assert c_rec.latency_delta_ms <= 0.0


def test_whatif_default_is_replicate_for_dense_workload(sparse_setup):
    g, cover, wls, placement, vol, lat = sparse_setup
    # A1 has no cross-group traffic in this fixture (no pairs referencing it) →
    # default 'replicate' recommendation.
    recs = whatif(g, cover, wls, placement, vol, lat, no_data_nodes=(), cfg=_cfg())
    by_uid = {r.workload_uid: r for r in recs}
    assert by_uid["d/Deployment/A1"].action == "replicate"
    assert by_uid["d/Deployment/A1"].confidence == "medium"


def test_whatif_no_data_node_emits_low_confidence_replicate():
    g = nx.Graph()
    g.add_edge("N1", "N2", weight_ms=2.0, count=1)
    cover = [frozenset({"N1", "N2"}), frozenset({"N3"})]
    wls = [
        {"workload_uid": "d/Deployment/lonely", "name": "lonely", "kind": "Deployment",
         "namespace": "d"},
    ]
    placement = {"d/Deployment/lonely": "N3"}
    recs = whatif(g, cover, wls, placement, {}, {}, no_data_nodes=("N3",), cfg=_cfg())
    assert len(recs) == 1
    r = recs[0]
    assert r.action == "replicate"
    assert r.confidence == "low"
    assert "insufficient" in r.summary_text.lower()


def test_whatif_excludes_daemonsets_from_candidates(sparse_setup):
    g, cover, wls, placement, vol, lat = sparse_setup
    # Turn A2 into a DaemonSet — it should NOT appear in the recs.
    wls2 = [dict(w, kind="DaemonSet") if w["workload_uid"].endswith("A2") else w for w in wls]
    recs = whatif(g, cover, wls2, placement, vol, lat, no_data_nodes=(), cfg=_cfg())
    assert not any(r.workload_uid.endswith("A2") for r in recs)


def test_whatif_migrate_cost_saving_computed_when_provider_present(sparse_setup):
    g, cover, wls, placement, vol, lat = sparse_setup

    def cost_of(uid):
        return 100.0 if uid == "d/Deployment/C" else None

    recs = whatif(g, cover, wls, placement, vol, lat, no_data_nodes=(),
                  cfg=_cfg(), cost_provider=cost_of)
    c_rec = next(r for r in recs if r.workload_uid == "d/Deployment/C")
    assert c_rec.action == "migrate"
    # C would otherwise be replicated across 2 peer groups (0 and 1) → cost 200; after
    # migration → 1 instance → cost 100. Savings 100.
    assert c_rec.cost_saving_amount == pytest.approx(100.0)
    assert c_rec.savings_currency == "USD"
    assert c_rec.confidence == "high"


def test_whatif_snippet_defaults_to_node_affinity(sparse_setup):
    g, cover, wls, placement, vol, lat = sparse_setup
    recs = whatif(g, cover, wls, placement, vol, lat, no_data_nodes=(), cfg=_cfg())
    c_rec = next(r for r in recs if r.workload_uid == "d/Deployment/C")
    assert c_rec.suggested_mechanism == "node_affinity"
    assert "nodeAffinity" in c_rec.suggested_snippet
    assert "kubernetes.io/hostname" in c_rec.suggested_snippet
