"""Shared analysis_core types.

Recommender-specific result types live under their head. This module holds only
small primitives reused across heads (kept from the vendored scaffolding for
continuity).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Interval:
    """A contiguous time window, as timestamps."""
    start: datetime
    end: datetime
