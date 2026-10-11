"""Shared result type: what every engine (rules, LLM, Kev) must return."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from smartplant.contract import Action, Urgency

Route = Literal["standard_cmms", "specialist_review"]
Source = Literal["kev", "jev", "adapter", "fake", "fallback", "safety_rules", "rules"]


@dataclass(frozen=True, slots=True)
class Recommendation:
    asset_id: str
    urgency: Urgency
    action: Action
    severity: int                         # 1..5
    route: Route
    requires_approval: bool
    source: str
    reasons: tuple[str, ...]
    explanation: str
    snapshot_hash: str
    contract_version: str
    model: str
    latency_ms: float = 0.0
    confidence: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        return d
