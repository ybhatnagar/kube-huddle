"""FastAPI app serving the /api/v1 surface for the Kube Huddle (docs/04 C).

Covers clusters + cached discovery, data sources, settings, collections (via the
collector trigger service), and analysis runs. The latency recommender head is
wired in M1; until then POST /runs returns 501. Live cluster/namespace discovery
needs a Kubernetes client and returns 501 on `refresh=true`; otherwise discovery
is served from the cache the collector populated.
"""
from __future__ import annotations

import os
import urllib.request
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..analysis_core.io.statestore import StateStore
from ..collector import CollectorUnavailable, trigger_collection, wait_for_collection
from ..kube import probe as kube_probe
from ..runner import run_analysis
from . import dto

StoreFactory = Callable[[], StateStore]


# --- request bodies --------------------------------------------------------

class RunRequest(BaseModel):
    cluster_id: Optional[int] = None
    cluster: Optional[str] = None
    scope: Any = "all"
    config: Optional[dict] = None
    collectData: bool = False
    ttl: Optional[str] = None
    run_type: str = "latency"


class ClusterCreate(BaseModel):
    name: str
    api_url: Optional[str] = None
    auth_method: Optional[str] = None
    credential_ref: Optional[str] = None
    ca_cert: Optional[str] = None


class ClusterTest(BaseModel):
    """Body for a live connectivity probe that does NOT persist a cluster (test-before-save)."""
    api_url: Optional[str] = None
    auth_method: Optional[str] = None
    credential_ref: Optional[str] = None
    ca_cert: Optional[str] = None


class SourceCreate(BaseModel):
    type: str
    name: str
    endpoint: Optional[str] = None
    auth_config: Optional[dict] = None
    settings: Optional[dict] = None
    enabled: bool = True


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    endpoint: Optional[str] = None
    enabled: Optional[bool] = None
    auth_config: Optional[dict] = None
    settings: Optional[dict] = None


class SettingsUpdate(BaseModel):
    metric_ttl_hours: Optional[int] = None
    discovery_ttl_min: Optional[int] = None
    result_ttl_hours: Optional[int] = None
    default_resources: Optional[str] = None
    default_window: Optional[str] = None
    thresholds: Optional[dict] = None


class CollectionRequest(BaseModel):
    cluster_id: Optional[int] = None
    cluster: Optional[str] = None
    scope: Any = "all"
    resources: Optional[list] = None
    window: Optional[str] = None
    interaction_source: Optional[str] = None


def _default_store_factory() -> StateStore:
    return StateStore(
        driver=os.environ.get("KUBEHUDDLE_DB_DRIVER", "sqlite"),
        dsn=os.environ.get("KUBEHUDDLE_DB_DSN", "./kubehuddle.db"),
    )


def _parse_ttl_hours(ttl: Optional[str]) -> int:
    if not ttl:
        return 24
    s = str(ttl).strip().lower()
    try:
        if s.endswith("h"):
            return int(float(s[:-1]))
        if s.endswith("d"):
            return int(float(s[:-1]) * 24)
        return int(float(s))
    except ValueError:
        return 24


def _probe_prometheus(endpoint: Optional[str]) -> str:
    if not endpoint:
        return "unknown"
    url = endpoint.rstrip("/") + "/api/v1/query?query=vector(1)"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            return "healthy" if resp.status == 200 else "unreachable"
    except Exception:
        return "unreachable"


def create_app(get_store: Optional[StoreFactory] = None) -> FastAPI:
    get_store = get_store or _default_store_factory
    app = FastAPI(title="Kube Huddle -- Engine API", version="0.1.0")

    origins = [o.strip() for o in os.environ.get("KUBEHUDDLE_CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def with_store(fn):
        store = get_store()
        try:
            return fn(store)
        finally:
            store.close()

    # --- health -----------------------------------------------------------

    @app.get("/api/v1/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # --- clusters ---------------------------------------------------------

    @app.post("/api/v1/clusters", status_code=201)
    def create_cluster(body: ClusterCreate) -> dict:
        def op(store):
            if store._fetchone("SELECT id FROM clusters WHERE name = ?", (body.name,)):
                raise HTTPException(status_code=409, detail=f"cluster {body.name!r} already exists")
            return dto.cluster_dto(store.create_cluster(
                body.name, api_url=body.api_url, auth_method=body.auth_method,
                credential_ref=body.credential_ref, ca_cert=body.ca_cert))
        return with_store(op)

    @app.get("/api/v1/clusters")
    def list_clusters() -> dict:
        return with_store(lambda s: {"clusters": [dto.cluster_dto(r) for r in s.list_clusters()]})

    @app.get("/api/v1/clusters/{cluster_id}")
    def get_cluster(cluster_id: int) -> dict:
        def op(store):
            row = dto.cluster_dto(store.get_cluster(cluster_id))
            if row is None:
                raise HTTPException(status_code=404, detail="cluster not found")
            return row
        return with_store(op)

    @app.delete("/api/v1/clusters/{cluster_id}")
    def delete_cluster(cluster_id: int) -> dict:
        def op(store):
            if not store.delete_cluster(cluster_id):
                raise HTTPException(status_code=404, detail="cluster not found")
            return {"deleted": True}
        return with_store(op)

    @app.post("/api/v1/clusters:test")
    def test_connection(body: ClusterTest) -> dict:
        """Live connectivity probe using the supplied fields -- does NOT save a cluster.

        Requires at least an api_url or a credential_ref: with neither, the probe would
        silently fall back to the cluster the engine runs in and misleadingly report
        "reachable" for empty input.
        """
        if not (body.api_url or body.credential_ref):
            return {"reachable": False,
                    "detail": "enter an API server URL and/or a credential Secret reference to test"}
        return kube_probe(api_url=body.api_url, auth_method=body.auth_method,
                          credential_ref=body.credential_ref, ca_cert=body.ca_cert)

    @app.post("/api/v1/clusters/{cluster_id}:test")
    def test_cluster(cluster_id: int) -> dict:
        """Live connectivity probe for a saved cluster (reads its stored api_url/auth)."""
        def op(store):
            row = store.get_cluster(cluster_id)
            if row is None:
                raise HTTPException(status_code=404, detail="cluster not found")
            result = kube_probe(api_url=row.get("api_url"), auth_method=row.get("auth_method"),
                                credential_ref=row.get("credential_ref"), ca_cert=row.get("ca_cert"))
            # Persist the outcome so the card shows current status.
            store.update_cluster_status(cluster_id, "reachable" if result.get("reachable") else "unreachable",
                                        touch=bool(result.get("reachable")))
            return {"id": str(cluster_id), **result}
        return with_store(op)

    # --- discovery (served from the cache) --------------------------------

    @app.get("/api/v1/clusters/{cluster_id}/namespaces")
    def list_namespaces(cluster_id: int, refresh: bool = Query(False)) -> dict:
        if refresh:
            raise HTTPException(status_code=501, detail="live discovery refresh needs a k8s client; returns cached data")
        return with_store(lambda s: {"namespaces": s.list_namespaces(cluster_id)})

    @app.get("/api/v1/clusters/{cluster_id}/namespaces/{namespace}/workloads")
    def list_workloads(cluster_id: int, namespace: str, refresh: bool = Query(False)) -> dict:
        if refresh:
            raise HTTPException(status_code=501, detail="live discovery refresh needs a k8s client; returns cached data")
        return with_store(lambda s: {"workloads": [dto.workload_dto(r) for r in s.list_workloads(cluster_id, namespace)]})

    # --- data sources -----------------------------------------------------

    @app.post("/api/v1/clusters/{cluster_id}/data_sources", status_code=201)
    def create_source(cluster_id: int, body: SourceCreate) -> dict:
        def op(store):
            if store.get_cluster(cluster_id) is None:
                raise HTTPException(status_code=404, detail="cluster not found")
            return dto.data_source_dto(store.create_data_source(
                cluster_id, body.type, body.name, endpoint=body.endpoint,
                auth_config=body.auth_config, settings=body.settings, enabled=body.enabled))
        return with_store(op)

    @app.get("/api/v1/clusters/{cluster_id}/data_sources")
    def list_sources(cluster_id: int) -> dict:
        return with_store(lambda s: {"sources": [dto.data_source_dto(r) for r in s.list_data_sources(cluster_id)]})

    @app.put("/api/v1/data_sources/{source_id}")
    def update_source(source_id: int, body: SourceUpdate) -> dict:
        def op(store):
            if store.get_data_source(source_id) is None:
                raise HTTPException(status_code=404, detail="source not found")
            return dto.data_source_dto(store.update_data_source(source_id, **body.model_dump(exclude_none=True)))
        return with_store(op)

    @app.delete("/api/v1/data_sources/{source_id}")
    def delete_source(source_id: int) -> dict:
        def op(store):
            if not store.delete_data_source(source_id):
                raise HTTPException(status_code=404, detail="source not found")
            return {"deleted": True}
        return with_store(op)

    @app.post("/api/v1/data_sources/{source_id}:test")
    def test_source(source_id: int) -> dict:
        def op(store):
            src = store.get_data_source(source_id)
            if src is None:
                raise HTTPException(status_code=404, detail="source not found")
            health = _probe_prometheus(src.get("endpoint")) if src["type"] in ("prometheus", "opencost") else "unknown"
            store.set_source_health(source_id, health)
            return {"id": str(source_id), "type": src["type"], "health": health}
        return with_store(op)

    # --- settings ---------------------------------------------------------

    @app.get("/api/v1/settings")
    def get_settings() -> dict:
        return with_store(lambda s: dto.settings_dto(s.get_settings()))

    @app.put("/api/v1/settings")
    def put_settings(body: SettingsUpdate) -> dict:
        return with_store(lambda s: dto.settings_dto(s.update_settings(**body.model_dump(exclude_none=True))))

    # --- collection -------------------------------------------------------

    @app.post("/api/v1/collections")
    def post_collection(body: CollectionRequest) -> dict:
        def op(store):
            cluster_id = body.cluster_id if body.cluster_id is not None else store.ensure_cluster(body.cluster or "default")
            try:
                res = trigger_collection(
                    cluster_id, body.scope, body.resources or ["cpu", "memory"], body.window or "7d",
                    interaction_source=body.interaction_source)
            except CollectorUnavailable as exc:
                raise HTTPException(status_code=503, detail=f"collector service unavailable: {exc}")
            return {"collection_id": str(res["collection_id"]), "status": res.get("status", "running")}
        return with_store(op)

    @app.get("/api/v1/collections/{collection_id}")
    def get_collection(collection_id: int) -> dict:
        def op(store):
            row = dto.collection_dto(store.get_collection_run(collection_id))
            if row is None:
                raise HTTPException(status_code=404, detail="collection not found")
            return row
        return with_store(op)

    # --- runs -------------------------------------------------------------

    @app.post("/api/v1/runs")
    def post_run(req: RunRequest) -> dict:
        def op(store):
            cluster = req.cluster_id if req.cluster_id is not None else (req.cluster or "default")
            ttl_hours = _parse_ttl_hours(req.ttl)
            try:
                res = run_analysis(
                    store, cluster=cluster, scope=req.scope,
                    config_overrides=req.config, ttl_hours=ttl_hours,
                    run_type=req.run_type,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {
                "run_id": str(res.run_id), "name": res.name, "status": res.status,
                "groups": res.groups, "recommendations": res.recommendations,
                "data_as_of": res.data_as_of, "stale": res.stale,
            }
        return with_store(op)

    @app.get("/api/v1/runs")
    def list_runs(cluster_id: Optional[int] = Query(None), limit: int = Query(50)) -> dict:
        return with_store(lambda s: {"runs": [dto.run_summary_dto(r) for r in s.list_runs(cluster_id, limit)]})

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: int) -> dict:
        def op(store):
            status = dto.run_status_dto(store, run_id)
            if status is None:
                raise HTTPException(status_code=404, detail="run not found")
            return status
        return with_store(op)

    # --- latency-run payloads (docs/04 §E) -------------------------------

    def _ensure_run(store, run_id: int) -> None:
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")

    @app.get("/api/v1/runs/{run_id}/groups")
    def get_run_groups(run_id: int) -> dict:
        def op(store):
            _ensure_run(store, run_id)
            return {"groups": dto.latency_groups_dto(store, run_id)}
        return with_store(op)

    @app.get("/api/v1/runs/{run_id}/graph")
    def get_run_graph(run_id: int) -> dict:
        def op(store):
            _ensure_run(store, run_id)
            return dto.graph_dto(store, run_id)
        return with_store(op)

    @app.get("/api/v1/runs/{run_id}/recommendations")
    def get_run_recommendations(run_id: int) -> dict:
        def op(store):
            _ensure_run(store, run_id)
            return {"recommendations": dto.latency_recommendations_dto(store, run_id)}
        return with_store(op)

    @app.get("/api/v1/runs/{run_id}/recommendations/{rec_id}/evidence")
    def get_rec_evidence(run_id: int, rec_id: int) -> dict:
        def op(store):
            _ensure_run(store, run_id)
            body = dto.evidence_dto(store, rec_id)
            if body is None:
                raise HTTPException(status_code=404, detail="recommendation not found")
            return body
        return with_store(op)

    # Optionally serve the static UI from the same origin (dev/demo). Set KUBEHUDDLE_UI_DIR.
    ui_dir = os.environ.get("KUBEHUDDLE_UI_DIR")
    if ui_dir and os.path.isdir(ui_dir):
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")

    return app


# Module-level app for `uvicorn engine.api.app:app`.
app = create_app()
