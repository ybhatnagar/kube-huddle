"""M4 contract tests: /runs/{id}/{groups,graph,recommendations,evidence} return
the exact docs/04 §E shapes on a seeded run.

Uses `TestClient` over a shared SQLite DB — same pattern as `test_api.py`, but
each test seeds a fixture + runs the latency head so the endpoints have real
data to serve.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from engine.analysis_core.io.statestore import StateStore
from engine.api.app import create_app
from engine.runner import run_analysis
from engine.synth import (
    fig2_cluster, no_data_cluster, seed_latency_cluster, sparse_migration_cluster,
)


@pytest.fixture
def client_and_store(tmp_path):
    dsn = str(tmp_path / "m4.db")
    seed_store = StateStore(driver="sqlite", dsn=dsn)
    seed_store.apply_schema()
    yield TestClient(create_app(lambda: StateStore(driver="sqlite", dsn=dsn))), seed_store
    seed_store.close()


def _run(store, fixture, *, cost_provider=None):
    fx = fixture()
    cid = seed_latency_cluster(store, fx)
    return run_analysis(store, cluster=cid, run_type="latency", cost_provider=cost_provider)


# --- GET /runs/{id}/groups -------------------------------------------------

def test_groups_endpoint_shape_matches_docs_04_e(client_and_store):
    client, store = client_and_store
    res = _run(store, fig2_cluster)
    body = client.get(f"/api/v1/runs/{res.run_id}/groups").json()

    assert "groups" in body
    assert len(body["groups"]) == 5   # Fig.2 -> 5-group cover
    g = body["groups"][0]
    assert set(g.keys()) >= {
        "group_id", "group_index", "label", "node_names", "latency_ratio",
        "workload_count", "total_size", "color_hint", "workloads",
    }
    assert isinstance(g["node_names"], list)
    assert isinstance(g["workloads"], list)
    # Every group's members carry the WorkloadOrbDTO shape.
    all_workloads = [w for gg in body["groups"] for w in gg["workloads"]]
    assert all_workloads, "at least one workload placed on a node"
    w = all_workloads[0]
    assert set(w.keys()) == {
        "workload_uid", "name", "namespace", "kind", "node_name", "size", "size_metric",
    }


def test_groups_endpoint_404_for_unknown_run(client_and_store):
    client, _ = client_and_store
    r = client.get("/api/v1/runs/9999/groups")
    assert r.status_code == 404


# --- GET /runs/{id}/graph --------------------------------------------------

def test_graph_endpoint_shape_matches_docs_04_e(client_and_store):
    client, store = client_and_store
    res = _run(store, fig2_cluster)
    body = client.get(f"/api/v1/runs/{res.run_id}/graph").json()

    assert set(body.keys()) == {"nodes", "edges"}
    n = body["nodes"][0]
    assert set(n.keys()) == {"id", "group_index"}
    assert body["edges"], "Fig.2 has edges"
    e = body["edges"][0]
    assert set(e.keys()) == {"a", "b", "latency_ms", "count", "kind"}
    assert e["kind"] in ("internal", "external")
    # Every edge's endpoints appear in the nodes list.
    node_ids = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["a"] in node_ids
        assert e["b"] in node_ids


# --- GET /runs/{id}/recommendations ---------------------------------------

def test_recommendations_endpoint_shape_and_action_vocab(client_and_store):
    client, store = client_and_store
    res = _run(store, sparse_migration_cluster,
               cost_provider=lambda uid: 100.0 if uid.endswith("/C") else None)
    body = client.get(f"/api/v1/runs/{res.run_id}/recommendations").json()

    assert "recommendations" in body
    recs = body["recommendations"]
    assert len(recs) >= 1
    r0 = recs[0]
    assert set(r0.keys()) >= {
        "recommendation_id", "workload", "action", "from", "to",
        "cost_saving", "latency_delta_ms",
        "suggested_mechanism", "suggested_snippet",
        "confidence", "summary",
    }
    # Nested `workload` shape.
    assert set(r0["workload"].keys()) == {"kind", "name", "namespace"}
    # Action vocabulary.
    for r in recs:
        assert r["action"] in ("migrate", "replicate")
        # from/to are either null or the {scope, label, group_index} shape.
        for side in (r["from"], r["to"]):
            if side is not None:
                assert set(side.keys()) == {"scope", "label", "group_index"}

    # The migrate rec for app C carries a cost_saving object + a snippet.
    migrate_recs = [r for r in recs if r["action"] == "migrate"]
    assert migrate_recs, "sparse fixture must produce at least one migrate rec"
    m = migrate_recs[0]
    assert m["cost_saving"] is not None
    assert set(m["cost_saving"].keys()) == {"amount", "currency", "period"}
    assert m["suggested_mechanism"] == "node_affinity"
    assert "nodeAffinity" in m["suggested_snippet"]


# --- GET /runs/{id}/recommendations/{recId}/evidence ----------------------

def test_evidence_endpoint_shape(client_and_store):
    client, store = client_and_store
    res = _run(store, sparse_migration_cluster,
               cost_provider=lambda uid: 100.0 if uid.endswith("/C") else None)

    # Pick the migrate rec (the interesting evidence case).
    recs = client.get(f"/api/v1/runs/{res.run_id}/recommendations").json()["recommendations"]
    migrate = next(r for r in recs if r["action"] == "migrate")
    rec_id = migrate["recommendation_id"]

    body = client.get(f"/api/v1/runs/{res.run_id}/recommendations/{rec_id}/evidence").json()
    assert set(body.keys()) == {"recommendation_id", "subgraph", "detail", "peers"}
    assert set(body["subgraph"].keys()) == {"nodes", "edges"}
    # detail carries volume/latency before/after — cost fields are optional.
    for k in ("volume_before", "volume_after", "latency_before", "latency_after"):
        assert k in body["detail"]

    # Peers list follows PeerDTO.
    assert body["peers"], "migrate rec should have peers recorded"
    p = body["peers"][0]
    assert set(p.keys()) >= {"peer_workload", "relation", "avg_count", "latency_ms"}


def test_evidence_endpoint_404_for_unknown_recommendation(client_and_store):
    client, store = client_and_store
    res = _run(store, fig2_cluster)
    r = client.get(f"/api/v1/runs/{res.run_id}/recommendations/9999999/evidence")
    assert r.status_code == 404


# --- No-data fixture: replicate low-confidence + still valid DTO shape ----

def test_no_data_run_still_serves_docs04_shaped_dtos(client_and_store):
    client, store = client_and_store
    res = _run(store, no_data_cluster)
    groups = client.get(f"/api/v1/runs/{res.run_id}/groups").json()["groups"]
    # 1 group ({N1,N2}); N3 is a no-data node — the runner emits a
    # low-confidence replicate rec but doesn't put N3 in a group.
    node_names = {n for g in groups for n in g["node_names"]}
    assert "N1" in node_names and "N2" in node_names
    assert "N3" not in node_names

    recs = client.get(f"/api/v1/runs/{res.run_id}/recommendations").json()["recommendations"]
    lonely = next(r for r in recs if r["workload"]["name"] == "lonely")
    assert lonely["action"] == "replicate"
    assert lonely["confidence"] == "low"
