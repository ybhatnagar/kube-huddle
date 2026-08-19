"""Optional LLM "why" hook — SCAFFOLD ONLY, OFF BY DEFAULT.

The deterministic templates in `templates.py` are always authoritative. This hook is
a clean seam for a future opt-in natural-language rephrasing; it is never wired into
the runner in this milestone and never replaces the deterministic math (docs/05).
"""
from __future__ import annotations

from typing import Optional, Protocol


class LLMExplainer(Protocol):
    def explain(self, context: dict) -> str: ...


def maybe_explain(context: dict, explainer: Optional[LLMExplainer] = None) -> Optional[str]:
    """Return an LLM rephrasing only if an explainer is explicitly provided; else None."""
    if explainer is None:
        return None
    return explainer.explain(context)  # pragma: no cover - not wired in M2
