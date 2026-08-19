"""Stage 7 — interaction peer expansion (pure helpers).

For a recommended workload, the runner walks its outgoing dependency edges, recovers
each peer's Signal, and asks these functions two things: does the peer share the same
period, and do their active windows line up in time? If both hold, the peer is suggested
alongside the primary recommendation. These functions also feed the target heuristic:
inbound traffic that fires during a workload's *idle* windows marks it as "ad-hoc"
(scale-to-zero rather than a fixed schedule). No DB access here — the runner loads data.
"""
from __future__ import annotations

import pandas as pd


def shares_period(a_hours: float, b_hours: float, tolerance: float) -> bool:
    """True if two periods agree within a relative tolerance."""
    return a_hours > 0 and b_hours > 0 and abs(a_hours - b_hours) / a_hours <= tolerance


def _common(a: pd.Series, b: pd.Series):
    idx = a.index.intersection(b.index)
    if len(idx) == 0:
        return None, None
    aa = a.reindex(idx).fillna(False).to_numpy().astype(bool)
    bb = b.reindex(idx).fillna(False).to_numpy().astype(bool)
    return aa, bb


def windows_align(a_active: pd.Series, b_active: pd.Series, min_overlap: float) -> tuple:
    """Jaccard overlap of two active masks over their shared time range → (aligned, frac)."""
    aa, bb = _common(a_active, b_active)
    if aa is None:
        return False, 0.0
    union = int((aa | bb).sum())
    frac = float((aa & bb).sum()) / union if union else 0.0
    return frac >= min_overlap, frac


def adhoc_overlap(target_active: pd.Series, source_active: pd.Series) -> float:
    """Fraction of the source's active time that lands in the target's *idle* windows."""
    tt, ss = _common(target_active, source_active)
    if tt is None:
        return 0.0
    denom = int(ss.sum())
    return float((ss & ~tt).sum()) / denom if denom else 0.0


def has_adhoc_inbound(target_active: pd.Series, incoming_active: list, min_frac: float = 0.5) -> bool:
    """True if any inbound caller is active mostly while the target is idle."""
    return any(adhoc_overlap(target_active, s) >= min_frac for s in incoming_active)


# --- interactions as a pseudo-resource -------------------------------------

def interaction_sum(edges: list, peer_uids=None) -> float:
    """Sum of an app's interaction counts.

    `edges` is a list of interaction rows (dicts with an ``avg_count`` and the other
    endpoint under ``dst_workload_uid`` and/or ``src_workload_uid``); the runner
    passes the app's outgoing edges. When ``peer_uids`` is given, only edges whose
    other endpoint is in that set are counted, so ranking stays *within the cluster*
    ("sum of all interaction counts for that app with others in the cluster",
    docs/01 Phase B.2). Returns 0.0 for an app with no (in-group) edges.
    """
    total = 0.0
    for e in edges:
        other = e.get("dst_workload_uid") or e.get("src_workload_uid")
        if peer_uids is not None and other is not None and other not in peer_uids:
            continue
        count = e.get("avg_count")
        if count is not None:
            total += float(count)
    return total
