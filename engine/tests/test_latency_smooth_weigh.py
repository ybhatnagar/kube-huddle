"""Unit tests for smoothed_latency, pick_alpha, i_window_from_unit,
build_node_graph (incl. DaemonSet edge exclusion)."""
from __future__ import annotations

import pytest

from engine.recommenders.latency.smooth import (
    i_window_from_unit, pick_alpha, smoothed_latency,
)
from engine.recommenders.latency.weigh import build_node_graph


def test_smoothed_empty_series_is_zero():
    assert smoothed_latency([], alpha=0.5, i_window=10) == 0.0


def test_smoothed_single_point_returns_that_point():
    assert smoothed_latency([7.5], alpha=0.5, i_window=10) == 7.5


def test_smoothed_alpha_zero_returns_moving_avg_only():
    # window = last 3 before the final -> mean(1,2,3) = 2.0
    assert smoothed_latency([1.0, 2.0, 3.0, 99.0], alpha=0.0, i_window=3) == 2.0


def test_smoothed_alpha_one_returns_latest_only():
    assert smoothed_latency([1.0, 2.0, 3.0, 99.0], alpha=1.0, i_window=3) == 99.0


def test_smoothed_i_window_larger_than_history_uses_what_exists():
    # window = last 100 before the final, but only 3 exist -> mean(1,2,3) = 2.0
    # blended: 0.5 * 4.0 + 0.5 * 2.0 = 3.0
    assert smoothed_latency([1.0, 2.0, 3.0, 4.0], alpha=0.5, i_window=100) == 3.0


def test_build_node_graph_averages_pair_latencies_per_node_pair():
    """Two app pairs cross between N1 and N2 with latencies 4 and 6;
    W_{N1,N2} = (4+6)/2 = 5, count = 2. Same-node pairs are ignored."""
    pair_latencies = {
        ("a1", "b1"): 4.0,   # a1@N1, b1@N2
        ("a2", "b2"): 6.0,   # a2@N1, b2@N2
        ("a1", "a2"): 3.0,   # both on N1 -> skipped
    }
    placement = {"a1": "N1", "a2": "N1", "b1": "N2", "b2": "N2"}
    g, no_data = build_node_graph(pair_latencies, placement, all_nodes=("N1", "N2", "N3"))
    assert set(g.nodes()) == {"N1", "N2"}
    assert g["N1"]["N2"]["weight_ms"] == pytest.approx(5.0)
    assert g["N1"]["N2"]["count"] == 2
    assert no_data == ("N3",)


def test_build_node_graph_skips_pairs_with_unknown_placement():
    pair_latencies = {("a", "b"): 5.0, ("a", "c"): 9.0}
    placement = {"a": "N1", "b": "N2"}   # c missing
    g, no_data = build_node_graph(pair_latencies, placement)
    assert list(g.edges()) == [("N1", "N2")]
    assert no_data == ()


def test_build_node_graph_excludes_daemonset_edges_by_default():
    """Directive: DaemonSets already run on every node, so their pair latencies
    don't represent scheduling latency the engine should optimize against.
    Every pair touching a DaemonSet workload_uid should be dropped."""
    pair_latencies = {
        ("app", "helper"): 3.0,   # both non-DS → kept
        ("app", "log-agent"): 5.0,  # log-agent is DS → dropped
        ("log-agent", "helper"): 7.0,   # log-agent is DS → dropped
    }
    placement = {"app": "N1", "helper": "N2", "log-agent": "N2"}
    daemonset_uids = frozenset({"log-agent"})

    g, _ = build_node_graph(pair_latencies, placement, daemonset_uids=daemonset_uids)
    assert set(g.edges()) == {("N1", "N2")}
    # Only the (app,helper) pair contributed, so weight is exactly 3.0.
    assert g["N1"]["N2"]["weight_ms"] == pytest.approx(3.0)
    assert g["N1"]["N2"]["count"] == 1


def test_build_node_graph_can_include_daemonset_edges_when_disabled():
    pair_latencies = {("app", "log-agent"): 5.0}
    placement = {"app": "N1", "log-agent": "N2"}
    g, _ = build_node_graph(
        pair_latencies, placement,
        daemonset_uids=frozenset({"log-agent"}),
        exclude_daemonset_edges=False,
    )
    assert list(g.edges()) == [("N1", "N2")]


def test_pick_alpha_high_for_steady_series_low_for_noisy():
    steady = [10.0, 10.0, 10.05, 9.95, 10.0]
    noisy = [1.0, 200.0, 3.0, 150.0, 5.0]
    # Steady → CV near 0 → α near ceiling.
    assert pick_alpha(steady) > pick_alpha(noisy)
    # Both stay within the configured clamp.
    for s in (steady, noisy):
        alpha = pick_alpha(s)
        assert 0.2 <= alpha <= 0.8


def test_i_window_from_unit_converts_time_to_sample_count():
    # Sample interval 1h. 6 hours → 6 samples. 30 min → 1 (min-clamp when <0.5).
    assert i_window_from_unit(6, "hours", sample_interval_seconds=3600) == 6
    assert i_window_from_unit(0, "hours", sample_interval_seconds=3600) == 1   # min-clamp
    # samples unit is a no-op.
    assert i_window_from_unit(10, "samples", sample_interval_seconds=3600) == 10
    # 2 days at 6h step = 8 samples.
    assert i_window_from_unit(2, "days", sample_interval_seconds=6 * 3600) == 8
    # unknown unit → treated as samples.
    assert i_window_from_unit(7, "fortnights", sample_interval_seconds=3600) == 7
