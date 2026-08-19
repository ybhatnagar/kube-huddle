"""Downsample a utilization series for the Why-panel chart (docs/04 §E, docs/05).

Cheap stride-based decimation to ~max_points, returned as [{"t": iso, "v": float}].
The full-fidelity LTTB variant can swap in behind this signature later.
"""
from __future__ import annotations

import math

import pandas as pd


def downsample(series: pd.Series, max_points: int = 200) -> list:
    """Return up to `max_points` (timestamp, value) points as JSON-ready dicts."""
    if series is None or len(series) == 0:
        return []
    n = len(series)
    step = max(1, math.ceil(n / max_points))
    out = []
    for i in range(0, n, step):
        ts = series.index[i]
        out.append({"t": pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ"), "v": float(series.iloc[i])})
    return out
