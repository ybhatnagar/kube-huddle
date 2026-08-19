"""Stage 4 — replicate-vs-migrate + cost, pure functions (docs/05 §B.4).

For each candidate workload (DaemonSets excluded per Yash's directive), decide
between:

  - `replicate` (default) — one instance per detected low-latency group,
    including a low-confidence "insufficient data" rec when the workload sits
    on a no-data node.
  - `migrate` — if the workload's cross-group traffic is small *and* the
    hypothetical single-instance placement in the dominant group doesn't
    worsen total network time × volume, and (if cost data is available) drops
    cost — the tool emits a migrate suggestion with `latency_delta_ms`,
    `cost_saving_amount`, and a copy-pasteable YAML snippet keyed to the
    target island's nodes. **The tool never applies it** — the user does.

Migration mechanism: `nodeAffinity` (preferred/soft) is the default per docs/05
§B.4 build-time direction; `nodeSelector` and `topologySpreadConstraints`
render when the config picks them. Open for Yash's confirmation at review.

Pure function — no store I/O; the runner does all reads/writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import networkx as nx

CostProvider = Callable[[str], Optional[float]]   # workload_uid -> per-instance cost (or None)
Pair = Tuple[str, str]


@dataclass(frozen=True)
class Recommendation:
    """One row destined for the latency_recommendations table (docs/04 §B)."""
    workload_uid: str
    action: str                                       # 'replicate' | 'migrate'
    confidence: str                                   # 'high' | 'medium' | 'low'
    summary_text: str
    workload_kind: Optional[str] = None
    workload_name: Optional[str] = None
    namespace: Optional[str] = None
    from_scope: Optional[str] = None
    to_scope: Optional[str] = None
    from_group_index: Optional[int] = None
    to_group_index: Optional[int] = None
    cost_saving_amount: Optional[float] = None
    savings_currency: Optional[str] = None
    savings_period: Optional[str] = None
    latency_delta_ms: Optional[float] = None
    suggested_mechanism: Optional[str] = None
    suggested_snippet: Optional[str] = None
    # Peers included for the evidence panel (surfaced by GET .../evidence).
    peers: Tuple[Tuple[str, float, float], ...] = field(default_factory=tuple)
    # (peer_workload_uid, avg_count, smoothed_latency_ms)


@dataclass(frozen=True)
class WhatifConfig:
    """A minimal subset of LatencyConfig — pass what whatif() actually needs."""
    enable_migration: bool = True
    migration_min_volume: float = 5.0
    migration_cost_weight: float = 1.0
    exclude_daemonsets: bool = True
    migration_mechanism: str = "node_affinity"     # 'node_affinity'|'node_selector'|'topology_spread'
    cost_enabled: bool = True
    cost_currency: str = "USD"
    cost_period: str = "month"


def whatif(
    graph: nx.Graph,
    cover: Sequence[frozenset],
    workloads: Sequence[Mapping],                  # each: {workload_uid, kind, name, namespace}
    placement: Mapping[str, str],                  # workload_uid -> node_name
    pair_volumes: Mapping[Pair, float],            # (src, dst) -> avg_count
    pair_smoothed: Mapping[Pair, float],           # (src, dst) -> smoothed_latency_ms
    no_data_nodes: Iterable[str] = (),
    *,
    cfg: WhatifConfig,
    cost_provider: Optional[CostProvider] = None,
) -> List[Recommendation]:
    """Emit one recommendation per candidate workload."""
    node_to_group_index: Dict[str, int] = {
        node: idx for idx, group in enumerate(cover) for node in group
    }
    group_labels = [", ".join(sorted(g)) for g in cover]

    candidates = [
        w for w in workloads
        if not (cfg.exclude_daemonsets and (w.get("kind") == "DaemonSet"))
    ]
    no_data_set: Set[str] = set(no_data_nodes)
    recs: List[Recommendation] = []

    for wl in candidates:
        uid = wl["workload_uid"]
        home_node = placement.get(uid)
        home_group = node_to_group_index.get(home_node) if home_node else None

        # --- Insufficient-data path: workload sits on a no-data node ---
        if home_node in no_data_set:
            recs.append(Recommendation(
                workload_uid=uid,
                workload_kind=wl.get("kind"), workload_name=wl.get("name"),
                namespace=wl.get("namespace"),
                action="replicate",
                from_scope=home_node, to_scope=home_node,
                from_group_index=home_group, to_group_index=None,
                confidence="low",
                summary_text=f"Insufficient interaction data on node {home_node} — replicate here.",
                suggested_mechanism=cfg.migration_mechanism,
                suggested_snippet=_render_snippet(cfg.migration_mechanism, [home_node]),
            ))
            continue

        # --- Interaction accounting ---
        vols_by_group, latency_before, peers = _account_pairs(
            uid, placement, node_to_group_index, pair_volumes, pair_smoothed,
        )
        total_vol = sum(vols_by_group.values())

        # --- Migration test ---
        migrate_rec = None
        if cfg.enable_migration and vols_by_group and total_vol > 0.0:
            migrate_rec = _try_migration(
                wl, home_group, vols_by_group, latency_before,
                cover, node_to_group_index, placement,
                pair_volumes, pair_smoothed, peers,
                cfg=cfg, cost_provider=cost_provider,
            )
        if migrate_rec is not None:
            recs.append(migrate_rec)
            continue

        # --- Default: replicate one per detected group ---
        recs.append(Recommendation(
            workload_uid=uid,
            workload_kind=wl.get("kind"), workload_name=wl.get("name"),
            namespace=wl.get("namespace"),
            action="replicate",
            from_scope=home_node, to_scope="all_groups",
            from_group_index=home_group, to_group_index=None,
            confidence="medium" if home_group is not None else "low",
            summary_text=(
                f"Replicate one instance per detected low-latency group "
                f"({len(cover)} groups: {'; '.join(group_labels)})."
            ),
            suggested_mechanism=cfg.migration_mechanism,
            suggested_snippet=_render_snippet(cfg.migration_mechanism, sorted(node for g in cover for node in g)) if False else None,
            peers=peers,
        ))
    return recs


# --- helpers ---------------------------------------------------------------


def _account_pairs(
    uid: str,
    placement: Mapping[str, str],
    node_to_group_index: Mapping[str, int],
    pair_volumes: Mapping[Pair, float],
    pair_smoothed: Mapping[Pair, float],
) -> Tuple[Dict[int, float], float, Tuple[Tuple[str, float, float], ...]]:
    """Return (volumes-per-peer-group, Σ vol*latency, peer rows for evidence)."""
    vols_by_group: Dict[int, float] = {}
    latency_before = 0.0
    peers: List[Tuple[str, float, float]] = []

    for (src, dst), vol in pair_volumes.items():
        if src == uid:
            other = dst
        elif dst == uid:
            other = src
        else:
            continue
        other_node = placement.get(other)
        if other_node is None:
            continue
        gi = node_to_group_index.get(other_node)
        if gi is None:
            continue
        lat = pair_smoothed.get((src, dst), pair_smoothed.get((dst, src), 0.0))
        vols_by_group[gi] = vols_by_group.get(gi, 0.0) + vol
        latency_before += vol * lat
        peers.append((other, vol, lat))
    return vols_by_group, latency_before, tuple(peers)


def _try_migration(
    wl: Mapping,
    home_group: Optional[int],
    vols_by_group: Dict[int, float],
    latency_before: float,
    cover: Sequence[frozenset],
    node_to_group_index: Mapping[str, int],
    placement: Mapping[str, str],
    pair_volumes: Mapping[Pair, float],
    pair_smoothed: Mapping[Pair, float],
    peers: Tuple[Tuple[str, float, float], ...],
    *,
    cfg: WhatifConfig,
    cost_provider: Optional[CostProvider],
) -> Optional[Recommendation]:
    """Evaluate migrating `wl` into its dominant peer group. Return a rec
    if the migration is beneficial under the config's thresholds, else None."""
    uid = wl["workload_uid"]
    dominant_group, dominant_vol = max(vols_by_group.items(), key=lambda kv: kv[1])
    total_vol = sum(vols_by_group.values())
    cross_group_vol = total_vol - dominant_vol
    if cross_group_vol > cfg.migration_min_volume:
        return None
    if home_group is not None and dominant_group == home_group:
        # Already in the dominant group — nothing to migrate.
        return None

    # Latency after: pairs whose peer sits in the dominant group become
    # intra-group (treat as 0); everything else keeps its current latency
    # (rough proxy — a rigorous optimizer lands in a later iteration).
    latency_after = 0.0
    for (src, dst), vol in pair_volumes.items():
        if src == uid:
            other = dst
        elif dst == uid:
            other = src
        else:
            continue
        peer_group = node_to_group_index.get(placement.get(other, ""))
        if peer_group is None:
            continue
        if peer_group == dominant_group:
            continue   # intra-group after migration → ~0 latency
        latency_after += vol * pair_smoothed.get((src, dst), pair_smoothed.get((dst, src), 0.0))

    latency_delta = latency_after - latency_before   # negative = improvement
    # Tolerate small worsening if configured (cost_weight adjusts strictness).
    if latency_delta > cfg.migration_cost_weight:
        return None

    # Cost accounting (optional).
    cost_saving = None
    if cfg.cost_enabled and cost_provider is not None:
        per_instance = cost_provider(uid)
        if per_instance is not None:
            # Replicate baseline: one instance per group the workload interacts with
            # (docs/05 §B.4: "Default = replicate one instance per group it
            # interacts with"). Home group is only included if the workload has
            # peer traffic there — otherwise a single home replica is redundant
            # once the workload has moved away.
            groups_touched = max(1, len(vols_by_group))
            cost_before = per_instance * groups_touched
            cost_after = per_instance * 1
            cost_saving = cost_before - cost_after
            if cost_saving <= 0:
                return None

    target_nodes = sorted(cover[dominant_group])
    snippet = _render_snippet(cfg.migration_mechanism, target_nodes)
    summary = (
        f"Migrate into group {dominant_group} ({', '.join(target_nodes)}): "
        f"low cross-group volume ({cross_group_vol:.1f}/hr), net Δlatency×vol "
        f"{latency_delta:+.1f} ms·calls"
        + (f", saves {cost_saving:.2f} {cfg.cost_currency}/{cfg.cost_period}" if cost_saving else "")
        + "."
    )
    return Recommendation(
        workload_uid=uid,
        workload_kind=wl.get("kind"), workload_name=wl.get("name"),
        namespace=wl.get("namespace"),
        action="migrate",
        from_scope=placement.get(uid), to_scope=", ".join(target_nodes),
        from_group_index=home_group, to_group_index=dominant_group,
        cost_saving_amount=cost_saving,
        savings_currency=(cfg.cost_currency if cost_saving is not None else None),
        savings_period=(cfg.cost_period if cost_saving is not None else None),
        latency_delta_ms=latency_delta,
        suggested_mechanism=cfg.migration_mechanism,
        suggested_snippet=snippet,
        confidence="high" if cost_saving is not None else "medium",
        summary_text=summary,
        peers=peers,
    )


def _render_snippet(mechanism: str, target_nodes: Sequence[str]) -> str:
    """Render a copy-pasteable YAML snippet keyed to `target_nodes`.

    Recommend-only — the tool never applies this. The snippet slots into the
    workload's `spec.template.spec` (or the top-level `spec` for `nodeSelector`).

    Mechanism default: `node_affinity` (preferred, soft) — declarative, matches
    on `kubernetes.io/hostname`, doesn't hard-fail scheduling when nodes drain.
    Alternative mechanisms are placeholders until Yash confirms the M3 default.
    """
    nodes_yaml = "\n".join(f"                - {n}" for n in target_nodes)
    if mechanism == "node_affinity":
        return (
            "affinity:\n"
            "  nodeAffinity:\n"
            "    preferredDuringSchedulingIgnoredDuringExecution:\n"
            "      - weight: 100\n"
            "        preference:\n"
            "          matchExpressions:\n"
            "            - key: kubernetes.io/hostname\n"
            "              operator: In\n"
            "              values:\n"
            f"{nodes_yaml}\n"
        )
    if mechanism == "node_selector":
        # nodeSelector only supports one value per key — emit guidance instead
        # of a broken snippet. Users typically add a shared label on the island's
        # nodes (e.g. `kubehuddle.io/island: A`) and select that.
        return (
            "# nodeSelector supports only single-value keys. Suggested pattern:\n"
            "# 1) Label the target island's nodes:\n"
            f"#    kubectl label node {target_nodes[0] if target_nodes else '<node>'} "
            "kubehuddle.io/island=A\n"
            "# 2) Add nodeSelector:\n"
            "nodeSelector:\n"
            "  kubehuddle.io/island: A\n"
        )
    if mechanism == "topology_spread":
        return (
            "topologySpreadConstraints:\n"
            "  - maxSkew: 1\n"
            "    topologyKey: kubehuddle.io/island\n"
            "    whenUnsatisfiable: ScheduleAnyway\n"
            "    labelSelector:\n"
            "      matchLabels:\n"
            "        app: <your-app-label>\n"
        )
    # Unknown mechanism → return a labelled placeholder rather than blowing up.
    return f"# unknown mechanism {mechanism!r}; target nodes: {', '.join(target_nodes)}\n"


__all__ = ["whatif", "Recommendation", "WhatifConfig", "CostProvider"]
