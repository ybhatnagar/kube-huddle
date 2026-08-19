"""API tests for the /api/v1 surface (FastAPI TestClient over a SQLite DB)."""
import pytest
from fastapi.testclient import TestClient

from engine.analysis_core.io.statestore import StateStore
from engine.api.app import create_app


@pytest.fixture
def client(tmp_path):
    dsn = str(tmp_path / "api.db")
    seed_store = StateStore(driver="sqlite", dsn=dsn)
    seed_store.apply_schema()
    # Create a cluster for tests that need one.
    seed_store.ensure_cluster("test-cluster")
    seed_store.close()

    app = create_app(lambda: StateStore(driver="sqlite", dsn=dsn))
    return TestClient(app)


def test_health_and_cluster_crud(client):
    c = client
    assert c.get("/api/v1/healthz").json()["status"] == "ok"
    created = c.post("/api/v1/clusters", json={"name": "prod-1", "api_url": "https://x:6443"})
    assert created.status_code == 201
    assert any(cl["name"] == "prod-1" for cl in c.get("/api/v1/clusters").json()["clusters"])
    # duplicate name -> 409
    assert c.post("/api/v1/clusters", json={"name": "prod-1"}).status_code == 409


def test_data_source_crud(client):
    c = client
    cid = c.post("/api/v1/clusters", json={"name": "c-ds"}).json()["id"]
    made = c.post(f"/api/v1/clusters/{cid}/data_sources",
                  json={"type": "prometheus", "name": "prom-main", "endpoint": "http://prom:9090"})
    assert made.status_code == 201, made.text
    assert made.json()["type"] == "prometheus"
    sid = made.json()["id"]
    upd = c.put(f"/api/v1/data_sources/{sid}", json={"enabled": False})
    assert upd.json()["enabled"] is False


def test_cluster_test_live_probe(client):
    c = client
    cid = c.get("/api/v1/clusters").json()["clusters"][0]["id"]
    # saved-cluster probe returns a structured live-probe result (unreachable in the
    # test env: cluster has no api_url and we're not running in a k8s pod).
    r = c.post(f"/api/v1/clusters/{cid}:test")
    assert r.status_code == 200, r.text
    assert r.json()["reachable"] is False and "detail" in r.json()
    # unknown cluster -> 404
    assert c.post("/api/v1/clusters/999999:test").status_code == 404
    # test-before-save (no persistence) against an unreachable endpoint -> structured False
    r2 = c.post("/api/v1/clusters:test", json={"api_url": "https://127.0.0.1:1", "auth_method": "token"})
    assert r2.status_code == 200 and r2.json()["reachable"] is False
    # and it did NOT create a cluster record
    assert all(cl["name"] != "127.0.0.1" for cl in c.get("/api/v1/clusters").json()["clusters"])
    # empty input must NOT silently probe the engine's own cluster -> guarded, not reachable
    empty = c.post("/api/v1/clusters:test", json={})
    assert empty.status_code == 200 and empty.json()["reachable"] is False
    assert "enter an API" in empty.json()["detail"].lower() or "credential" in empty.json()["detail"].lower()


def test_settings_roundtrip(client):
    c = client
    assert c.get("/api/v1/settings").json()["default_window"] == "7d"
    upd = c.put("/api/v1/settings", json={"default_window": "14d"})
    assert upd.json()["default_window"] == "14d"


def test_post_run_executes_latency_head(client):
    """POST /runs runs the latency head against an empty cluster — should
    succeed with 0 groups/recommendations (no interaction data seeded)."""
    c = client
    r = c.post("/api/v1/runs", json={"cluster": "test-cluster", "run_type": "latency"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["groups"] == 0
    assert body["recommendations"] == 0
    assert body.get("run_id")


def test_post_run_rejects_unknown_run_type(client):
    c = client
    r = c.post("/api/v1/runs", json={"cluster": "test-cluster", "run_type": "notatype"})
    assert r.status_code == 400
    assert "unknown run_type" in r.json()["detail"]
