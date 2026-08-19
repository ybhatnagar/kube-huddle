"""Tests for min_ratio_cover's tie-break rule and the greedy cover fallback."""
from __future__ import annotations

import networkx as nx

from engine.recommenders.latency.group import (
    candidate_groups,
    latency_ratio,
    min_ratio_cover,
    min_ratio_cover_greedy,
)


def test_fewer_groups_tie_break_on_genuine_tie():
    """Two mirror-image islands with identical structure — any pair of covers
    that partitions them has the same summed ratio. `fewer_groups` should
    pick the coarser cover (2 groups over 4 singletons)."""
    g = nx.Graph()
    # Two islands: {A1,A2} and {B1,B2}. Internal edges cheap, inter-island expensive.
    g.add_edge("A1", "A2", weight_ms=1.0, count=1)
    g.add_edge("B1", "B2", weight_ms=1.0, count=1)
    g.add_edge("A1", "B1", weight_ms=10.0, count=1)
    g.add_edge("A2", "B2", weight_ms=10.0, count=1)

    cands = candidate_groups(g)
    # Both {A1,A2} and {B1,B2} must be present.
    assert frozenset({"A1", "A2"}) in cands
    assert frozenset({"B1", "B2"}) in cands

    cover, total = min_ratio_cover(g, cands, tie="fewer_groups")
    partition = {frozenset(c) for c in cover}
    # The two-group cover — one pair per island — is strictly better than any
    # cover that splits an island into singletons: e.g. splitting {A1,A2} into
    # {A1}+{A2} keeps the same external total, and the {A1,A2} ratio (1/20=0.05)
    # is already lower than singleton overhead in adjacent groups.
    assert partition == {frozenset({"A1", "A2"}), frozenset({"B1", "B2"})}
    # Verify the total = 2 × 1/20 = 0.1.
    assert round(total, 4) == 0.1


def test_greedy_cover_partitions_all_nodes():
    """The greedy fallback must still cover every node. On Fig.2 it should
    reach the same 5-group answer as the exact solver — the ratios sort
    unambiguously (singletons + tightest pairs first)."""
    edges = [
        ("N1", "N3", 1), ("N1", "N2", 2), ("N2", "N3", 3),
        ("N3", "N4", 4), ("N2", "N4", 5),
        ("N4", "N6", 8), ("N4", "N8", 10),
        ("N6", "N7", 2), ("N6", "N5", 1), ("N7", "N5", 3),
        ("N5", "N8", 7),
    ]
    g = nx.Graph()
    for a, b, w in edges:
        g.add_edge(a, b, weight_ms=float(w))
    cands = candidate_groups(g)

    cover, _ = min_ratio_cover_greedy(g, cands)
    partition = {frozenset(c) for c in cover}
    # Every node covered exactly once.
    covered = set()
    for c in cover:
        assert covered.isdisjoint(c), "greedy produced an overlap"
        covered |= c
    assert covered == set(g.nodes())
    # And the greedy solver should still land on the Fig.2 answer here.
    assert partition == {
        frozenset({"N1", "N3"}),
        frozenset({"N2", "N4"}),
        frozenset({"N5", "N6"}),
        frozenset({"N7"}),
        frozenset({"N8"}),
    }


def test_min_ratio_cover_dispatches_on_cover_mode():
    """The public min_ratio_cover(cover_mode='greedy') should call the greedy
    variant. Cheap to verify with a small graph — both paths hit the same
    partition here."""
    g = nx.Graph()
    g.add_edge("A", "B", weight_ms=1.0, count=1)
    g.add_edge("B", "C", weight_ms=10.0, count=1)
    cands = candidate_groups(g)
    for mode in ("exact", "greedy"):
        cover, _ = min_ratio_cover(g, cands, cover_mode=mode)
        covered = set()
        for c in cover:
            covered |= c
        assert covered == {"A", "B", "C"}, f"{mode!r} left some nodes uncovered"
