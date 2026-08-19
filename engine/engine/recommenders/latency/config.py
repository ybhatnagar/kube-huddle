"""Latency engine config (docs/05 §C).

Frozen dataclass with all knobs defaulted; the runner passes the resulting
dict into `analysis_runs.config` so runs are reproducible.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Optional, Union


@dataclass(frozen=True)
class LatencyConfig:
    alpha: Union[float, str] = 0.5           # float in [0,1], or 'auto' to pick from series stdev
    i_window: int = 10
    i_unit: str = "samples"                    # 'samples'|'minutes'|'hours'|'days'
    group_by: str = "node"                     # 'node'|'<topology_label>'
    tie_break: str = "fewer_groups"
    round_sig_digits: int = 2
    cover_mode: str = "exact"                  # 'exact'|'greedy'
    enable_migration: bool = True
    migration_min_volume: float = 5.0
    migration_cost_weight: float = 1.0
    exclude_daemonsets: bool = True
    exclude_daemonset_edges: bool = True
    migration_mechanism: str = "node_affinity"  # 'node_affinity'|'node_selector'|'topology_spread'
    cost_enabled: bool = True
    cost_currency: str = "USD"
    cost_period: str = "month"
    orb_size_metric: str = "interaction_volume"

    def with_overrides(self, **kw: Any) -> "LatencyConfig":
        return replace(self, **{k: v for k, v in kw.items() if v is not None})

    def to_config_dict(self) -> dict:
        return asdict(self)


def config_from_overrides(overrides: Optional[dict]) -> LatencyConfig:
    cfg = LatencyConfig()
    if overrides:
        cfg = cfg.with_overrides(**overrides)
    return cfg
