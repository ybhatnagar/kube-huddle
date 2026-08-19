"""DTO assembly for the REST surface (docs/04 E). Pure dict builders over store rows."""
from __future__ import annotations

from typing import Optional

from ..analysis_core.io.statestore import StateStore, _iso, _parse_dt


# --- config / discovery ----------------------------------------------------

def cluster_dto(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {
        "id": str(row["id"]), "name": row["name"], "api_url": row.get("api_url"),
        "auth_method": row.get("auth_method"), "status": row.get("status"),
        "created_at": _iso(_parse_dt(row.get("created_at"))),
        "last_connected_at": _iso(_parse_dt(row.get("last_connected_at"))),
    }


def data_source_dto(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {
        "id": str(row["id"]), "cluster_id": row.get("cluster_id"), "type": row["type"],
        "name": row["name"], "endpoint": row.get("endpoint"), "enabled": bool(row.get("enabled")),
        "settings": row.get("settings"), "health": row.get("health"),
        "last_checked_at": _iso(_parse_dt(row.get("last_checked_at"))),
    }


def settings_dto(row: Optional[dict]) -> dict:
    row = row or {}
    return {k: row.get(k) for k in (
        "metric_ttl_hours", "discovery_ttl_min", "result_ttl_hours",
        "default_resources", "default_window", "thresholds")}


def collection_dto(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {
        "id": str(row["id"]), "status": row["status"],
        "progress": 100 if row["status"] in ("success", "failed", "partial") else 0,
        "data_as_of": _iso(_parse_dt(row.get("data_as_of"))),
        "rows_written": row.get("rows_written"), "error": row.get("error"),
    }


def workload_dto(row: dict) -> dict:
    return {
        "kind": row["kind"], "name": row["name"], "namespace": row["namespace"],
        "workload_uid": row.get("workload_uid"), "replicas": row.get("replicas"),
        "requests_cpu_m": row.get("requests_cpu_m"), "requests_mem_bytes": row.get("requests_mem_bytes"),
    }


# --- runs ------------------------------------------------------------------

def run_summary_dto(row: dict) -> dict:
    return {
        "id": str(row["id"]), "name": row["name"], "cluster_id": row.get("cluster_id"),
        "run_type": row.get("run_type", "latency"), "status": row["status"],
        "stale": bool(row.get("stale")), "data_as_of": _iso(_parse_dt(row.get("data_as_of"))),
        "created_at": _iso(_parse_dt(row.get("created_at"))), "completed_at": _iso(_parse_dt(row.get("completed_at"))),
    }


def run_status_dto(store: StateStore, run_id: int) -> Optional[dict]:
    run = store.get_run(run_id)
    if not run:
        return None
    return {
        "id": str(run["id"]), "run_type": run.get("run_type", "latency"), "status": run["status"],
        "data_as_of": _iso(_parse_dt(run.get("data_as_of"))), "stale": bool(run["stale"]),
        "progress": 100 if run["status"] in ("completed", "failed") else 0, "error": run.get("error"),
    }


# --- latency DTOs (docs/04 §E) ---------------------------------------------

def workload_orb_dto(row: dict) -> dict:
    """WorkloadOrbDTO: one workload nested inside a group for the orb view."""
    return {
        "workload_uid": row.get("workload_uid"),
        "name": row.get("workload_name"),
        "namespace": row.get("namespace"),
        "kind": row.get("kind"),
        "node_name": row.get("node_name"),
        "size": float(row.get("size_value") or 0.0),
        "size_metric": row.get("size_metric") or "interaction_volume",
    }


def latency_group_dto(row: dict, workloads: list[dict]) -> dict:
    """LatencyGroupDTO — one detected island with its member workloads."""
    ratio = row.get("latency_ratio")
    return {
        "group_id": str(row["id"]),
        "group_index": row["group_index"],
        "label": row.get("label") or ", ".join(row.get("node_names") or []),
        "node_names": row.get("node_names") or [],
        "latency_ratio": float(ratio) if ratio is not None else None,
        "workload_count": row.get("workload_count"),
        "total_size": (float(row["total_size"]) if row.get("total_size") is not None else None),
        "color_hint": row.get("color_hint"),
        "workloads": [workload_orb_dto(w) for w in workloads],
    }


def latency_groups_dto(store: StateStore, run_id: int) -> list[dict]:
    groups = store.list_latency_groups(run_id)
    members = store.list_latency_group_workloads(run_id)
    by_gid: dict[int, list[dict]] = {}
    for m in members:
        by_gid.setdefault(int(m["group_id"]), []).append(m)
    return [latency_group_dto(g, by_gid.get(int(g["id"]), [])) for g in groups]


def graph_dto(store: StateStore, run_id: int) -> dict:
    """GraphDTO: weighing-engine nodes + edges (drives showback + Why)."""
    groups = store.list_latency_groups(run_id)
    node_to_group: dict[str, int] = {}
    for g in groups:
        for n in (g.get("node_names") or []):
            node_to_group[n] = int(g["group_index"])
    edges = store.list_latency_edges(run_id)
    node_ids = set(node_to_group)
    for e in edges:
        node_ids.add(e["node_a"])
        node_ids.add(e["node_b"])
    return {
        "nodes": [{"id": n, "group_index": node_to_group.get(n)} for n in sorted(node_ids)],
        "edges": [
            {
                "a": e["node_a"], "b": e["node_b"],
                "latency_ms": float(e["weight_ms"]),
                "count": int(e.get("sample_count") or 0),
                "kind": e.get("kind"),
            } for e in edges
        ],
    }


def _scope(scope: Optional[str], group_index: Optional[int]) -> Optional[dict]:
    if scope is None and group_index is None:
        return None
    return {"scope": scope, "label": scope, "group_index": group_index}


def latency_recommendation_dto(row: dict) -> dict:
    """LatencyRecommendationDTO per docs/04 §E."""
    saving = row.get("cost_saving_amount")
    cost_saving = None
    if saving is not None:
        cost_saving = {
            "amount": float(saving),
            "currency": row.get("savings_currency"),
            "period": row.get("savings_period"),
        }
    return {
        "recommendation_id": str(row["id"]),
        "workload": {
            "kind": row.get("workload_kind"),
            "name": row.get("workload_name"),
            "namespace": row.get("namespace"),
        },
        "workload_uid": row.get("workload_uid"),
        "action": row["action"],
        "from": _scope(row.get("from_scope"), row.get("from_group_index")),
        "to": _scope(row.get("to_scope"), row.get("to_group_index")),
        "cost_saving": cost_saving,
        "latency_delta_ms": (float(row["latency_delta_ms"])
                              if row.get("latency_delta_ms") is not None else None),
        "suggested_mechanism": row.get("suggested_mechanism"),
        "suggested_snippet": row.get("suggested_snippet"),
        "confidence": row.get("confidence"),
        "summary": row.get("summary_text"),
    }


def latency_recommendations_dto(store: StateStore, run_id: int) -> list[dict]:
    return [latency_recommendation_dto(r) for r in store.list_latency_recommendations(run_id)]


def peer_dto(row: dict) -> dict:
    return {
        "peer_workload": row.get("peer_workload"),
        "peer_workload_uid": row.get("peer_workload_uid"),
        "relation": row.get("relation"),
        "avg_count": float(row["avg_count"]) if row.get("avg_count") is not None else None,
        "latency_ms": float(row["latency_ms"]) if row.get("latency_ms") is not None else None,
    }


def evidence_dto(store: StateStore, recommendation_id: int) -> Optional[dict]:
    """EvidenceDTO: subgraph + before/after detail + peer list."""
    rec = store.get_latency_recommendation(recommendation_id)
    if rec is None:
        return None
    ev = store.get_latency_evidence(recommendation_id) or {}
    peers = store.list_latency_peers(recommendation_id)
    return {
        "recommendation_id": str(recommendation_id),
        "subgraph": ev.get("subgraph") or {"nodes": [], "edges": []},
        "detail": ev.get("detail") or {
            "volume_before": None, "volume_after": None,
            "latency_before": None, "latency_after": None,
        },
        "peers": [peer_dto(p) for p in peers],
    }
