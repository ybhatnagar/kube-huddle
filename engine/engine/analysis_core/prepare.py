"""Stage 0 — Prepare: resample to hourly, handle gaps, coverage checks."""
from __future__ import annotations

import pandas as pd


def prepare_series(points: list[tuple], freq: str = "1h") -> pd.Series:
    """Build a regular, gap-filled series from raw (timestamp, value) points.

    Resamples to `freq` (mean within bucket), forward/back-fills small gaps.
    Returns an empty Series if there are no points.
    """
    if not points:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime([p[0] for p in points], utc=True)
    s = pd.Series([float(p[1]) for p in points], index=idx).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s = s.resample(freq).mean()
    # Interpolate short gaps; fill any residual edges so downstream stages see no NaN.
    s = s.interpolate(limit=2, limit_direction="both").ffill().bfill()
    s = s.dropna()
    return s


def has_min_coverage(series: pd.Series, period_hours: float, min_periods: int) -> bool:
    """True if the series spans at least `min_periods` full periods."""
    if series.empty or period_hours <= 0:
        return False
    return len(series) >= min_periods * period_hours
