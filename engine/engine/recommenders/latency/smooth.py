"""Stage 1 — exponential smoothing per the disclosure (docs/05 §B.1).

Pure functions; no I/O.

  - `smoothed_latency(series, alpha, i_window)` — the disclosure's rule.
  - `pick_alpha(series)` — heuristic auto-α from the series' stdev (higher
    variability → lean more on the mean, lower α; steady series → trust the
    latest, higher α). Used when the runner enables auto-α.
  - `i_window_from_unit(i_window, i_unit, sample_interval_seconds)` — convert
    an `i_window` expressed in time (`'minutes'|'hours'|'days'`) into a sample
    count so smoothed_latency can consume it uniformly. `'samples'` is a no-op.
"""
from __future__ import annotations

import math
from typing import Sequence


def smoothed_latency(series: Sequence[float], alpha: float, i_window: int) -> float:
    """SmoothedLatency = alpha * latest + (1 - alpha) * mean(last i values before latest).

    Guards:
      - empty series -> 0.0
      - single point -> that point (no history to average)
      - fewer than i_window prior points -> mean of what exists
    """
    if not series:
        return 0.0
    if len(series) == 1:
        return float(series[0])
    latest = float(series[-1])
    window = series[max(0, len(series) - 1 - i_window):-1]
    if not window:
        return latest
    moving_avg = sum(window) / len(window)
    return alpha * latest + (1.0 - alpha) * moving_avg


def pick_alpha(series: Sequence[float], *, floor: float = 0.2, ceiling: float = 0.8) -> float:
    """Heuristic α from the series' coefficient of variation (stdev/mean).

    Rationale (docs/05 §B.1 "Faithful extension"): a steady series (low CV)
    should trust the latest reading — high α. A noisy series (high CV) should
    lean on the moving average — low α. The mapping is `α = 1 / (1 + CV)`
    clamped to `[floor, ceiling]`; both extremes are configurable so the caller
    can widen or narrow the range for their environment.
    """
    if not series:
        return floor
    if len(series) < 2:
        return ceiling
    mean = sum(series) / len(series)
    if mean == 0:
        return ceiling
    var = sum((x - mean) ** 2 for x in series) / len(series)
    cv = math.sqrt(var) / abs(mean)
    alpha = 1.0 / (1.0 + cv)
    return max(floor, min(ceiling, alpha))


def i_window_from_unit(i_window: int, i_unit: str, sample_interval_seconds: float) -> int:
    """Convert an `i_window` in time units to a sample count.

    `i_unit` in {'samples','minutes','hours','days'}. `'samples'` returns
    `i_window` unchanged. Time-based units divide by `sample_interval_seconds`
    to get an integer sample count (minimum 1). Callers pass the collector's
    step (e.g. 3600s for '1h') as the sample interval.
    """
    if i_unit == "samples" or sample_interval_seconds <= 0:
        return max(1, int(i_window))
    seconds_per = {"minutes": 60.0, "hours": 3600.0, "days": 86400.0}.get(i_unit)
    if seconds_per is None:
        return max(1, int(i_window))
    total_seconds = float(i_window) * seconds_per
    return max(1, int(round(total_seconds / sample_interval_seconds)))
