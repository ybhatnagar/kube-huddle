"""Latency recommender head (M3 — faithful stages).

Reads collected data from the store, runs the four stages (smooth → weigh →
group → whatif), and writes latency_groups + latency_edges +
latency_recommendations (+ evidence peers). Pure algorithm lives in
{smooth,weigh,group,whatif}.py; this module handles I/O and orchestration only.
"""
from __future__ import annotations

import random
import string
from datetime import datetime, timezone
from typing import Any, Optional

from ...analysis_core.io.statestore import StateStore
from .config import LatencyConfig, config_from_overrides
from .group import candidate_groups, latency_ratio, min_ratio_cover
from .smooth import i_window_from_unit, pick_alpha, smoothed_latency
from .weigh import build_node_graph
from .whatif import WhatifConfig, whatif


def _slug() -> str:
    parts = ["brave", "clever", "bright", "swift", "quiet", "gentle", "sharp", "sunny"]
    animals = ["otter", "falcon", "lynx", "orca", "gecko", "raven", "koala", "heron"]
    n = "".join(random.choices(string.digits, k=4))
    return f"{random.choice(parts)}-{random.choice(animals)}-{n}"


def _resolve_cluster_id(store: StateStore, cluster: Any) -> int:
    if isinstance(cluster, int):
        return cluster
    return store.ensure_cluster(str(cluster))


def _whatif_cfg(cfg: LatencyConfig) -> WhatifConfig:
    return WhatifConfig(
        enable_migration=cfg.enable_migration,
        migration_min_volume=cfg.migration_min_volume,
        migration_cost_weight=cfg.migration_cost_weight,
        exclude_daemonsets=cfg.exclude_daemonsets,
        migration_mechanism=cfg.migration_mechanism,
        cost_enabled=cfg.cost_enabled,
        cost_currency=cfg.cost_currency,
        cost_period=cfg.cost_period,
    )


def run_latency_analysis(
    store: StateStore,
    *,
    cluster: Any,
    scope: Any = "all",
    config_overrides: Optional[dict] = None,
    ttl_hours: int = 24,
    name: Optional[str] = None,
    cost_provider=None,
    sample_interval_seconds: float = 3600.0,
    **_: Any,
):
    """Execute the latency head end-to-end. Returns a RunResult envelope."""
    from ...runner import RunResult   # local import to avoid a runner<->head cycle

    cfg: LatencyConfig = config_from_overrides(config_overrides)
    cluster_id = _resolve_cluster_id(store, cluster)
    run_name = name or _slug()

    data_as_of = store.max_collected_at(cluster_id) or datetime.now(timezone.utc)
    run_id = store.create_analysis_run(
        name=run_name, cluster_id=cluster_id, scope=scope,
        config=cfg.to_config_dict(), data_as_of=data_as_of,
        stale=False, ttl_hours=ttl_hours, run_type="latency",
    )

    try:
        # --- Stage 1: smooth ---
        pair_series = store.load_latency_series_by_pair(cluster_id)
        i_window = i_window_from_unit(cfg.i_window, cfg.i_unit, sample_interval_seconds)
        pair_smoothed: dict[tuple, float] = {}
        for pair, series in pair_series.items():
            alpha = pick_alpha(series) if cfg.alpha == "auto" else float(cfg.alpha)
            pair_smoothed[pair] = smoothed_latency(series, alpha=alpha, i_window=i_window)

        # --- Stage 2: weigh ---
        placement = store.load_pod_placements(cluster_id)
        workloads = store.list_all_workloads(cluster_id)
        daemonset_uids = frozenset(
            w["workload_uid"] for w in workloads if w.get("kind") == "DaemonSet"
        )
        all_nodes = tuple(sorted(set(placement.values())))
        graph, no_data_nodes = build_node_graph(
            pair_smoothed, placement, all_nodes=all_nodes,
            daemonset_uids=daemonset_uids,
            exclude_daemonset_edges=cfg.exclude_daemonset_edges,
        )

        # --- Stage 3: group ---
        candidates = candidate_groups(graph)
        cover, total_ratio = min_ratio_cover(
            graph, candidates,
            round_sig=cfg.round_sig_digits, tie=cfg.tie_break,
            cover_mode=cfg.cover_mode,
        )

        # --- Persist groups + edges ---
        wl_by_uid = {w["workload_uid"]: w for w in workloads}
        node_to_uids: dict[str, list[str]] = {}
        for uid, node in placement.items():
            node_to_uids.setdefault(node, []).append(uid)

        node_to_group_id: dict[str, int] = {}
        node_to_group_index: dict[str, int] = {}
        groups_written = 0
        for idx, group in enumerate(cover):
            group_ratio = latency_ratio(graph, group)
            members_sorted = sorted(group)
            grp_id = store.insert_latency_group(
                run_id=run_id, group_index=idx, node_names=members_sorted,
                latency_ratio=group_ratio, workload_count=sum(len(node_to_uids.get(n, [])) for n in members_sorted),
            )
            groups_written += 1
            for n in members_sorted:
                node_to_group_id[n] = grp_id
                node_to_group_index[n] = idx
                for uid in node_to_uids.get(n, []):
                    wl = wl_by_uid.get(uid, {})
                    store.insert_latency_group_workload(
                        group_id=grp_id, run_id=run_id, workload_uid=uid,
                        workload_name=wl.get("name"), namespace=wl.get("namespace"),
                        kind=wl.get("kind"), node_name=n, size_value=0.0,
                        size_metric=cfg.orb_size_metric,
                    )

        for u, v, data in graph.edges(data=True):
            gi = node_to_group_index.get(u)
            gj = node_to_group_index.get(v)
            same = gi is not None and gi == gj
            store.insert_latency_edge(
                run_id=run_id, node_a=u, node_b=v,
                weight_ms=float(data["weight_ms"]), sample_count=int(data.get("count", 0)),
                kind="internal" if same else "external",
                group_id=node_to_group_id[u] if same else None,
            )

        # --- Stage 4: whatif ---
        pair_volumes = store.load_interactions_volume(cluster_id)
        recs = whatif(
            graph, cover, workloads, placement, pair_volumes, pair_smoothed,
            no_data_nodes=no_data_nodes,
            cfg=_whatif_cfg(cfg),
            cost_provider=cost_provider,
        )
        recs_written = 0
        for r in recs:
            rec_id = store.insert_latency_recommendation(
                run_id=run_id, workload_uid=r.workload_uid,
                workload_kind=r.workload_kind, workload_name=r.workload_name,
                namespace=r.namespace,
                action=r.action,
                from_scope=r.from_scope, to_scope=r.to_scope,
                from_group_index=r.from_group_index, to_group_index=r.to_group_index,
                cost_saving_amount=r.cost_saving_amount,
                savings_currency=r.savings_currency, savings_period=r.savings_period,
                latency_delta_ms=r.latency_delta_ms,
                suggested_mechanism=r.suggested_mechanism,
                suggested_snippet=r.suggested_snippet,
                confidence=r.confidence, summary_text=r.summary_text,
            )
            recs_written += 1
            _persist_evidence(
                store, rec_id, r, graph, node_to_group_index, placement, wl_by_uid,
            )

        store.finish_analysis_run(run_id, status="completed")
        return RunResult(
            run_id=run_id, name=run_name, status="completed",
            recommendations=recs_written, groups=groups_written,
            data_as_of=data_as_of.isoformat() if data_as_of else None, stale=False,
        )
    except Exception as exc:  # ensure the run row records failure
        store.finish_analysis_run(run_id, status="failed", error=str(exc))
        raise


def _persist_evidence(store, rec_id, rec, graph, node_to_group_index, placement, wl_by_uid) -> None:
    """Write latency_peers rows + a latency_evidence subgraph for one rec.

    The subgraph is the sub-portion of the weighing-engine graph that touches
    the rec's workload — the rec's home node + all peer nodes + the edges
    between them. That's enough for the UI's 'Why' force-graph without pulling
    the whole cluster's graph on every /evidence call.
    """
    home_node = placement.get(rec.workload_uid)
    peer_uids = [p[0] for p in rec.peers]
    peer_nodes = {placement.get(u) for u in peer_uids if placement.get(u) is not None}
    subgraph_node_set = set()
    if home_node is not None:
        subgraph_node_set.add(home_node)
    subgraph_node_set |= peer_nodes
    subgraph_nodes = [
        {"id": n, "group_index": node_to_group_index.get(n)}
        for n in sorted(subgraph_node_set)
    ]
    subgraph_edges = []
    for u, v, data in graph.edges(data=True):
        if u in subgraph_node_set and v in subgraph_node_set:
            gi_u = node_to_group_index.get(u)
            gi_v = node_to_group_index.get(v)
            subgraph_edges.append({
                "a": u, "b": v,
                "latency_ms": float(data["weight_ms"]),
                "count": int(data.get("count", 0)),
                "kind": "internal" if (gi_u is not None and gi_u == gi_v) else "external",
            })

    volume_before = sum(v for _, v, _ in rec.peers)
    latency_before = sum(v * lat for _, v, lat in rec.peers)
    detail = {
        "volume_before": volume_before,
        "volume_after": volume_before,   # migration keeps the same call volume
        "latency_before": latency_before,
        "latency_after": (latency_before + rec.latency_delta_ms
                          if rec.latency_delta_ms is not None else latency_before),
    }
    if rec.cost_saving_amount is not None:
        # cost_after was cost_before − savings by construction in whatif.
        detail["cost_after"] = None
        detail["cost_before"] = rec.cost_saving_amount   # placeholder; real values wired with OpenCost
    store.insert_latency_evidence(rec_id, subgraph={"nodes": subgraph_nodes, "edges": subgraph_edges},
                                   detail=detail)

    for uid, avg_count, latency in rec.peers:
        peer_wl = wl_by_uid.get(uid, {})
        pretty = f"{peer_wl.get('namespace','?')}/{peer_wl.get('kind','?')}/{peer_wl.get('name', uid)}"
        store.insert_latency_peer(
            rec_id,
            peer_workload_uid=uid,
            peer_workload=pretty,
            relation="peer",
            avg_count=avg_count,
            latency_ms=latency,
        )


__all__ = ["run_latency_analysis"]
