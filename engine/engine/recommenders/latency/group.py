"""Stage 3 — grouping (the invention core, docs/05 §B.3, docs/01 §B Stages 3a–d).

Pure functions on a networkx.Graph with edge attr 'weight_ms'.

  - grow_group(G, start, allowed=None): greedy growth with constraints
    (a) admit the neighbour reachable by the LEAST edge weight;
    (b) that edge must be strictly less than the group's current max_edge
        (a singleton's first edge is always admissible).
    `allowed` restricts growth to a subset of nodes (used by the sequential
    cover — nodes already in earlier groups are excluded).

  - candidate_groups(G): for every start node, run the SEQUENTIAL cover
    process — grow one group, then start a fresh group from the next
    unvisited node (natural sorted order), repeat until all nodes are
    covered. Take the union of groups across all N covers. This matches
    docs/01 §B.3a-b: "start a new group from the next unvisited node …
    Repeat from every node as start point → a series of candidate node-group
    lists". Singletons are always available so a cover always exists.

  - latency_ratio(G, group): Σ internal / Σ external. Singleton or
    zero-external -> 0.0.

  - min_ratio_cover(G, candidates, round_sig, tie): exact backtracking
    cover minimizing Σ ratios; tie-break by fewer groups after rounding.
"""
from __future__ import annotations

import math
from typing import FrozenSet, Iterable, List, Optional, Set, Tuple

import networkx as nx


def _edge_w(g: nx.Graph, u: str, v: str) -> float:
    return float(g[u][v]["weight_ms"])


def grow_group(
    g: nx.Graph,
    start: str,
    allowed: Optional[Set[str]] = None,
) -> FrozenSet[str]:
    """Greedy growth from `start`, optionally restricted to `allowed` nodes."""
    if start not in g:
        return frozenset({start})
    group: Set[str] = {start}
    max_edge = -math.inf
    while True:
        best: Optional[Tuple[float, str]] = None
        for u in group:
            for v in g.neighbors(u):
                if v in group:
                    continue
                if allowed is not None and v not in allowed:
                    continue
                w = _edge_w(g, u, v)
                # Singleton (max_edge == -inf) always admits the first edge.
                if max_edge == -math.inf or w < max_edge:
                    if best is None or w < best[0]:
                        best = (w, v)
        if best is None:
            break
        w, v = best
        group.add(v)
        max_edge = max(max_edge, w)
    return frozenset(group)


def sequential_cover(g: nx.Graph, start: str) -> List[FrozenSet[str]]:
    """Grow the first group from `start`, then keep growing new groups from
    the next unvisited node (sorted order) until all nodes are covered."""
    all_nodes = sorted(g.nodes())
    unvisited: Set[str] = set(all_nodes)
    groups: List[FrozenSet[str]] = []
    # First group from the given start.
    if start in unvisited:
        grp = grow_group(g, start, allowed=unvisited)
        groups.append(grp)
        unvisited -= grp
    # Subsequent groups: pick the next unvisited node in sorted order.
    while unvisited:
        nxt = next(n for n in all_nodes if n in unvisited)
        grp = grow_group(g, nxt, allowed=unvisited)
        groups.append(grp)
        unvisited -= grp
    return groups


def candidate_groups(g: nx.Graph) -> List[FrozenSet[str]]:
    """Union of groups across the N sequential covers (one per start node).

    A singleton for an edged node is only a candidate when it arises naturally
    from a sequential cover (e.g. N7 in Fig.2, isolated after {N5,N6} takes
    its neighbours). Adding singletons for every node would make all-singletons
    the trivial minimum (each ratio=0), which is nonsense. Isolated nodes with
    zero edges are always emitted as singletons by grow_group itself.
    """
    seen: Set[FrozenSet[str]] = set()
    for start in g.nodes():
        seen.update(sequential_cover(g, start))
    return sorted(seen, key=lambda s: (-len(s), sorted(s)))


def latency_ratio(g: nx.Graph, group: Iterable[str]) -> float:
    """Σ W_internal / Σ W_external. Singleton or zero-external -> 0.0."""
    members = set(group)
    if len(members) <= 1:
        return 0.0
    internal = 0.0
    external = 0.0
    for u, v, data in g.edges(data=True):
        w = float(data["weight_ms"])
        u_in, v_in = u in members, v in members
        if u_in and v_in:
            internal += w
        elif u_in ^ v_in:
            external += w
    if external == 0.0:
        return 0.0
    return internal / external


def _round_sig(x: float, sig: int) -> float:
    if x == 0.0:
        return 0.0
    if math.isinf(x):
        return x
    d = math.floor(math.log10(abs(x)))
    return round(x, -(d - (sig - 1)))


def min_ratio_cover(
    g: nx.Graph,
    candidates: List[FrozenSet[str]],
    *,
    round_sig: int = 2,
    tie: str = "fewer_groups",
    cover_mode: str = "exact",
) -> Tuple[List[FrozenSet[str]], float]:
    """Dispatch: exact backtracking (small/medium graphs) or greedy set-cover
    fallback (large graphs). Cover_mode 'exact'|'greedy'.

    Both variants return (chosen_groups, total_ratio). Every G.node appears in
    exactly one group.
    """
    if cover_mode == "greedy":
        return min_ratio_cover_greedy(g, candidates)
    return min_ratio_cover_exact(g, candidates, round_sig=round_sig, tie=tie)


def min_ratio_cover_exact(
    g: nx.Graph,
    candidates: List[FrozenSet[str]],
    *,
    round_sig: int = 2,
    tie: str = "fewer_groups",
) -> Tuple[List[FrozenSet[str]], float]:
    """Exact minimum-ratio exact cover of all G.nodes().

    Ties (after rounding total to `round_sig` significant digits) broken by
    `tie`: 'fewer_groups' picks the cover with fewer groups. Backtracking with
    branch-and-bound pruning; feasibility guaranteed when singletons for every
    node are present in `candidates` (sequential_cover naturally emits them for
    nodes that get isolated by earlier picks).
    """
    all_nodes = set(g.nodes())
    cand_ratio: List[float] = [latency_ratio(g, c) for c in candidates]
    node_to_cand: dict[str, List[int]] = {n: [] for n in all_nodes}
    for idx, c in enumerate(candidates):
        for n in c:
            if n in all_nodes:
                node_to_cand.setdefault(n, []).append(idx)

    best_total = math.inf
    best_pick: List[int] = []

    def rec(covered: Set[str], picked: List[int], total: float) -> None:
        nonlocal best_total, best_pick
        # Rounded-tie prune: allow re-descent if we can still tie the current best.
        if _round_sig(total, round_sig) > _round_sig(best_total, round_sig):
            return
        remaining = all_nodes - covered
        if not remaining:
            r_total = _round_sig(total, round_sig)
            r_best = _round_sig(best_total, round_sig) if best_total != math.inf else math.inf
            better = r_total < r_best
            tied = r_total == r_best and best_pick and _prefers_tie(picked, best_pick, tie)
            if better or tied:
                best_total = total
                best_pick = list(picked)
            return
        pivot = min(remaining, key=lambda n: sum(1 for i in node_to_cand[n] if not (candidates[i] & covered)))
        options = [i for i in node_to_cand[pivot] if not (candidates[i] & covered)]
        options.sort(key=lambda i: cand_ratio[i])
        for i in options:
            picked.append(i)
            rec(covered | candidates[i], picked, total + cand_ratio[i])
            picked.pop()

    rec(set(), [], 0.0)
    return [candidates[i] for i in best_pick], best_total


def min_ratio_cover_greedy(
    g: nx.Graph,
    candidates: List[FrozenSet[str]],
) -> Tuple[List[FrozenSet[str]], float]:
    """Greedy lowest-ratio set-cover fallback (docs/05 §B.3).

    Repeatedly pick the still-fitting candidate with the lowest ratio-per-new-node
    score, breaking ties by size (larger groups win to reduce total groups).
    Not guaranteed optimal, but O(|cand|·|nodes|) instead of exponential — the
    exchange the runner makes on large graphs via `cover_mode='greedy'`.
    """
    all_nodes = set(g.nodes())
    covered: Set[str] = set()
    picked: List[FrozenSet[str]] = []
    total = 0.0

    # Precompute ratios once.
    scored = [(c, latency_ratio(g, c)) for c in candidates]

    while covered != all_nodes:
        best: Optional[Tuple[float, int, FrozenSet[str], float]] = None
        for c, r in scored:
            new_nodes = c - covered
            if not new_nodes:
                continue
            # Cost per newly-covered node — lower is better; tie-break by group
            # size descending (fewer, larger groups).
            key = (r / len(new_nodes), -len(new_nodes), c, r)
            if best is None or key < best:
                best = key
        if best is None:
            # No candidate can extend the cover — bail with what we have.
            break
        _, _, chosen, chosen_ratio = best
        picked.append(chosen)
        covered |= chosen
        total += chosen_ratio

    return picked, total


def _prefers_tie(new_pick: List[int], cur_pick: List[int], tie: str) -> bool:
    if tie == "fewer_groups":
        return len(new_pick) < len(cur_pick)
    return False
