"""Tests for the grouping stage on the disclosure's Fig.2 graph.

Verifies:
  - grow_group respects constraints (a) least-weight next, (b) strictly-lower
    than max_edge (with singleton bootstrap).
  - sequential_cover reproduces the disclosure's cover from a given start.
  - latency_ratio matches the disclosure's per-group table
    ({N1,N3}=1/9, {N5,N6}=1/20).
  - min_ratio_cover returns the (algorithmically) minimizing cover on Fig.2 at
    a total ratio near 0.35 (= 1/9 + 5/27 + 1/20).

Doc inconsistency noted in the M1 STOP summary: docs/01 and docs/05 both claim
the disclosure "chooses" the 4-group cover (N1,N2,N3,N4)(N5,N6)(N7)(N8) via a
tie-break. Under the formula stated in docs/01 §B.3c, {N1,N2,N3,N4} has ratio
15/18 = 0.833 so that 4-group cover sums to 0.88 — not 0.35, not a tie. The
algorithm implemented here is faithful to the FORMULA; the "chosen cover"
claim needs Yash's review.
"""
from __future__ import annotations

import networkx as nx
import pytest

from engine.recommenders.latency.group import (
    candidate_groups,
    grow_group,
    latency_ratio,
    min_ratio_cover,
    sequential_cover,
)


@pytest.fixture
def fig2_graph() -> nx.Graph:
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
    return g


def test_grow_group_from_N1_returns_N1_N3(fig2_graph):
    """N1's cheapest neighbour is N3 (1). After adding N3 (max_edge=1),
    every remaining neighbour edge is >= 2 so nothing more is admissible."""
    assert grow_group(fig2_graph, "N1") == frozenset({"N1", "N3"})


def test_grow_group_from_N4_returns_N1_N2_N3_N4(fig2_graph):
    """From N4: add N3 (4, max=4), then N1 via N3 (1 < 4), then N2 via N1
    (2 < 4). N6/N8 edges are >= 8, stop. This puts {N1,N2,N3,N4} in the pool
    even though grow_group(N1)/grow_group(N2)/grow_group(N3) don't yield it."""
    assert grow_group(fig2_graph, "N4") == frozenset({"N1", "N2", "N3", "N4"})


def test_sequential_cover_from_N1_matches_disclosure(fig2_graph):
    """Starting at N1: grow → {N1,N3}. Then N2 is the next unvisited node;
    grow on the remaining subgraph produces {N2,N4}. Then {N5,N6}, {N7}, {N8}.
    This is exactly the 5-group cover the disclosure worked-example shows."""
    cover = [set(g) for g in sequential_cover(fig2_graph, "N1")]
    assert cover == [
        {"N1", "N3"}, {"N2", "N4"}, {"N5", "N6"}, {"N7"}, {"N8"},
    ]


def test_latency_ratio_matches_disclosure_table(fig2_graph):
    # {N1,N3}: internal = 1 (N1-N3); external = 2+3+4 = 9. ratio = 1/9.
    assert latency_ratio(fig2_graph, {"N1", "N3"}) == pytest.approx(1 / 9)
    # {N5,N6}: internal = 1 (N5-N6); external = 3+7+2+8 = 20. ratio = 1/20.
    assert latency_ratio(fig2_graph, {"N5", "N6"}) == pytest.approx(1 / 20)
    # {N2,N4}: internal = 5; external = 2+3+4+8+10 = 27. ratio = 5/27.
    assert latency_ratio(fig2_graph, {"N2", "N4"}) == pytest.approx(5 / 27)
    # {N1,N2,N3,N4}: internal = 1+2+3+4+5 = 15; external = 8+10 = 18.
    assert latency_ratio(fig2_graph, {"N1", "N2", "N3", "N4"}) == pytest.approx(15 / 18)
    # Singleton with edges → ratio 0 by definition.
    assert latency_ratio(fig2_graph, {"N7"}) == 0.0


def test_candidate_groups_include_key_disclosure_shapes(fig2_graph):
    cands = set(candidate_groups(fig2_graph))
    # Groups the disclosure explicitly names.
    assert frozenset({"N1", "N3"}) in cands
    assert frozenset({"N2", "N4"}) in cands
    assert frozenset({"N5", "N6"}) in cands
    assert frozenset({"N7"}) in cands           # singleton emerges via sequential cover
    assert frozenset({"N8"}) in cands
    assert frozenset({"N1", "N2", "N3", "N4"}) in cands   # emerges when starting at N4


def test_min_ratio_cover_is_disclosure_five_group_at_035(fig2_graph):
    """The FORMULA-optimal cover on Fig.2 is the 5-group answer at 0.346:
    (N1,N3) + (N2,N4) + (N5,N6) + (N7) + (N8) = 1/9 + 5/27 + 1/20 ≈ 0.346.

    Docs/05 claims the algorithm should return the 4-group cover
    (N1,N2,N3,N4)(N5,N6)(N7)(N8) — but that cover sums to 0.88 under the
    same formula, so it can't win. See the M1 STOP summary."""
    cands = candidate_groups(fig2_graph)
    cover, total = min_ratio_cover(fig2_graph, cands)
    partition = {frozenset(g) for g in cover}
    assert partition == {
        frozenset({"N1", "N3"}),
        frozenset({"N2", "N4"}),
        frozenset({"N5", "N6"}),
        frozenset({"N7"}),
        frozenset({"N8"}),
    }
    assert total == pytest.approx(1 / 9 + 5 / 27 + 1 / 20)
    assert round(total, 2) == 0.35
