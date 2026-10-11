# capstone-project/src/triage/policy_engine.py
"""Turns probabilities into an allowed destination. All risk tolerance lives here, in numbers."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal, Mapping

from triage.contract import DispatchTarget, to_business_urgency
from triage.jev_client import DecisionResponse

POLICY_VERSION: Final[str] = "triage-policy/1.0.0"
Band = Literal["low", "medium", "high"]
Priority = Literal["P1", "P2", "P3", "P4"]


@dataclass(frozen=True, slots=True)
class Thresholds:
    low_floor: float = 0.50
    fraud_override: float = 0.85
    fraud_suspect: float = 0.50
    manipulation: float = 0.50
    min_conf: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({
        "tier1_support": 0.70,
        "secops_agent": 0.60,        # false positive is cheap
        "refund_pipeline": 0.90,     # money moves
        "manual_escalation": 0.0,
    }))
    high_band: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({
        "tier1_support": 0.85, "secops_agent": 0.80, "refund_pipeline": 0.95, "manual_escalation": 0.0,
    }))


DEFAULT_THRESHOLDS: Final = Thresholds()


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    destination: DispatchTarget
    band: Band
    priority: Priority
    urgency: int                      # business scale 1..5
    requires_confirmation: bool
    requires_human_approval: bool
    reasons: tuple[str, ...]
    policy_version: str = POLICY_VERSION


def _priority(urgency: int, fraud_p: float) -> Priority:
    if urgency >= 5 or (urgency >= 4 and fraud_p >= 0.5):
        return "P1"
    return "P2" if urgency >= 4 else "P3" if urgency >= 3 else "P4"


def decide(resp: DecisionResponse, state: Mapping[str, Any],
           t: Thresholds = DEFAULT_THRESHOLDS) -> PolicyDecision:
    fraud_p = resp.noul("is_fraud_suspected").p_yes
    urgency = to_business_urgency(resp.score("urgency_rating").score)
    target = resp.choice("dispatch_target")
    prio = _priority(urgency, fraud_p)
    reasons: list[str] = []

    # 1. Safety first: text that tries to steer automation never auto-routes.
    manip = resp.answers.get("contains_instructions_to_system")
    if manip is not None and resp.noul("contains_instructions_to_system").p_yes >= t.manipulation:
        return PolicyDecision("manual_escalation", "low", prio, urgency, False, True,
                              ("possible_manipulation",))

    # 2. Fraud signal overrides the model's own routing choice.
    if fraud_p >= t.fraud_override:
        reasons.append(f"fraud_override p={fraud_p:.2f}")
        return PolicyDecision("secops_agent", "high", prio, urgency, False, False, tuple(reasons))
    if fraud_p >= t.fraud_suspect:
        reasons.append(f"fraud_suspected p={fraud_p:.2f}")
        return PolicyDecision("secops_agent", "medium", prio, urgency, True, False, tuple(reasons))

    # 3. Per-destination confidence gate.
    dest: DispatchTarget = target.choice  # type: ignore[assignment]  # contract guarantees label set
    if target.confidence < t.low_floor or target.confidence < t.min_conf[dest]:
        reasons.append(f"low_confidence {dest}@{target.confidence:.2f} margin={target.margin():.2f}")
        return PolicyDecision("manual_escalation", "low", prio, urgency, False, True, tuple(reasons))

    band: Band = "high" if target.confidence >= t.high_band[dest] else "medium"

    # 4. Hard business rules: deterministic, never delegated to the model.
    needs_approval = False
    if dest == "refund_pipeline":
        amount_band = state.get("computed_signals", {}).get("disputed_amount_band")
        if amount_band == "above_50k":
            needs_approval = True
            reasons.append("refund_above_50k_requires_approval")
    reasons.append(f"{dest}@{target.confidence:.2f}")
    return PolicyDecision(dest, band, prio, urgency, band == "medium", needs_approval, tuple(reasons))
