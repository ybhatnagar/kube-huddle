"""Runner dispatch: pick the recommender head by `run_type`.

The latency head is wired in M1. Add more heads by mapping `run_type` to a
callable with the same signature as `run_latency_analysis`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .analysis_core.io.statestore import StateStore
from .recommenders.latency.runner import run_latency_analysis


@dataclass
class RunResult:
    """Minimal result envelope returned by a recommender head."""
    run_id: int
    name: str
    status: str
    recommendations: int
    groups: int
    data_as_of: Optional[str]
    stale: bool


def run_analysis(
    store: StateStore,
    *,
    cluster: Any,
    scope: Any = "all",
    config_overrides: Optional[dict] = None,
    ttl_hours: int = 24,
    name: Optional[str] = None,
    run_type: str = "latency",
    **kwargs,
) -> RunResult:
    """Dispatch to the recommender head for `run_type`."""
    if run_type == "latency":
        return run_latency_analysis(
            store, cluster=cluster, scope=scope,
            config_overrides=config_overrides, ttl_hours=ttl_hours, name=name,
            **kwargs,
        )
    raise ValueError(f"unknown run_type: {run_type!r}")


__all__ = ["run_analysis", "RunResult"]
