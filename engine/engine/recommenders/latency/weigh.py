"""Stage 2 — weighing engine (docs/05 §B.2).

Build an undirected node graph from per-pair smoothed latencies + a workload->node
placement map. W_ij = mean of ∂_xy across app pairs x@i, y@j.

Optionally excludes edges where either endpoint is a DaemonSet workload — Yash's
directive: a DaemonSet already runs on every node, so its cross-node "calls" don't
represent scheduling latency the engine should optimize against.
"""
from __future__ import annotations

from typing import AbstractSet, Iterable, Mapping, Tuple

import networkx as nx

PairLatencies = Mapping[Tuple[str, str], float]   # (src_uid, dst_uid) -> smoothed ms
Placement = Mapping[str, str]                     # workload_uid -> node_name


def build_node_graph(
    pair_latencies: PairLatencies,
    placement: Placement,
    *,
    all_nodes: Iterable[str] = (),
    daemonset_uids: AbstractSet[str] = frozenset(),
    exclude_daemonset_edges: bool = True,
) -> Tuple[nx.Graph, Tuple[str, ...]]:
    """Return (graph, no_data_nodes).

    Same-node pairs, or pairs whose endpoints are missing from `placement`, are
    skipped. Pairs where either endpoint is in `daemonset_uids` are also skipped
    when `exclude_daemonset_edges=True` (the default). `all_nodes` (optional)
    is the full node inventory used to detect no-data nodes.

    Edge attrs: weight_ms (float), count (int).
    """
    accum: dict[Tuple[str, str], list[float]] = {}
    for (src, dst), lat in pair_latencies.items():
        if exclude_daemonset_edges and (src in daemonset_uids or dst in daemonset_uids):
            continue
        n_i = placement.get(src)
        n_j = placement.get(dst)
        if n_i is None or n_j is None or n_i == n_j:
            continue
        a, b = (n_i, n_j) if n_i <= n_j else (n_j, n_i)
        accum.setdefault((a, b), []).append(float(lat))

    g = nx.Graph()
    for (a, b), lats in accum.items():
        g.add_edge(a, b, weight_ms=sum(lats) / len(lats), count=len(lats))

    graph_nodes = set(g.nodes())
    no_data = tuple(n for n in all_nodes if n not in graph_nodes)
    return g, no_data
