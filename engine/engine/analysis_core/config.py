"""Engine configuration -- latency-specific config added in M1."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class EngineConfig:
    """Base config; latency knobs added in recommenders/latency/config.py."""
    window: str = "7d"

    def with_overrides(self, **kw: Any) -> "EngineConfig":
        from dataclasses import replace
        clean = {k: v for k, v in kw.items() if v is not None and k in self.__dataclass_fields__}
        return replace(self, **clean)

    @classmethod
    def from_settings(cls, settings: dict | None) -> "EngineConfig":
        if not settings:
            return cls()
        kw = {}
        if settings.get("default_window"):
            kw["window"] = settings["default_window"]
        return cls(**kw)

    def to_config_dict(self) -> dict:
        return {"window": self.window}
