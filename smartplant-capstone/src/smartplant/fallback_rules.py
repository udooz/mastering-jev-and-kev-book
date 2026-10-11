"""Deterministic local rules: the safety floor, the Kev-down fallback, and the rule-based baseline.

These run on the edge with no model and no network. They are conservative on purpose:
when in doubt they inspect and escalate, and they never recommend TRANSFER_LOAD.
"""
from __future__ import annotations

from typing import Any, Mapping

from smartplant.contract import CONTRACT_VERSION, Action, Urgency
from smartplant.models import Recommendation

ACTION_RANK: dict[str, int] = {
    "MONITOR": 0, "INSPECT": 1, "SCHEDULE_MAINTENANCE": 2, "TRANSFER_LOAD": 3, "REQUEST_SHUTDOWN": 4,
}
URGENCY_RANK: dict[str, int] = {"ROUTINE": 0, "PRIORITY": 1, "URGENT": 2, "CRITICAL": 3}


def safety_floor(state: Mapping[str, Any]) -> tuple[Urgency, Action] | None:
    """Minimum response implied by code-computed facts. A model may escalate past it, never below it."""
    t = state["telemetry"]
    if state["protective_limit_reached"] or t["temperature_band"] == "trip":
        return "CRITICAL", "REQUEST_SHUTDOWN"
    if t["vibration_zone"] == "D_danger" or (
            t["temperature_band"] == "high" and t["temperature_trend"] == "rising_fast"):
        return "URGENT", "INSPECT"
    if t["temperature_band"] == "high" or t["vibration_zone"] == "C_alert":
        return "PRIORITY", "INSPECT"
    return None


def rule_baseline(state: Mapping[str, Any], *, source: str = "rules", reason: str = "rule_baseline") -> Recommendation:
    """Static thresholds and if/else: Engine A in the Chapter 12 benchmark, and the Kev-down fallback."""
    t, dq = state["telemetry"], state["data_quality"]["status"]
    reasons = [reason]
    floor = safety_floor(state)
    if floor and floor[1] == "REQUEST_SHUTDOWN":
        urgency, action, sev, route = "CRITICAL", "REQUEST_SHUTDOWN", 5, "specialist_review"
        reasons.append("protective_limit_reached: PLC trip is authoritative")
    elif dq == "invalid":
        urgency, action, sev, route = "PRIORITY", "INSPECT", 3, "specialist_review"
        reasons.append(f"untrusted_data: {','.join(state['data_quality']['issues'])}")
    elif floor:
        urgency, action = floor
        sev = 4 if urgency == "URGENT" else 3
        route = "specialist_review" if urgency == "URGENT" else "standard_cmms"
        reasons.append(f"threshold: vib={t['vibration_zone']} temp={t['temperature_band']}/{t['temperature_trend']}")
    elif t["vibration_trend"] in ("rising", "rising_fast") or (
            t["temperature_band"] == "elevated" and t["temperature_trend"] in ("rising", "rising_fast")):
        urgency, action, sev, route = "PRIORITY", "INSPECT", 2, "standard_cmms"
        reasons.append("early_degradation_trend")
    else:
        urgency, action, sev, route = "ROUTINE", "MONITOR", 1, "standard_cmms"
    if dq == "degraded" and route == "standard_cmms" and action != "MONITOR":
        route = "specialist_review"
        reasons.append("degraded_data")
    return Recommendation(
        asset_id=state["asset"]["id"], urgency=urgency, action=action, severity=sev, route=route,  # type: ignore[arg-type]
        requires_approval=action in ("TRANSFER_LOAD", "REQUEST_SHUTDOWN"), source=source,
        reasons=tuple(reasons), explanation=explain(state, action, urgency, reasons),  # type: ignore[arg-type]
        snapshot_hash=state["snapshot_hash"], contract_version=CONTRACT_VERSION, model="rules-1.0",
    )


def fallback_recommendation(state: Mapping[str, Any], why: str) -> Recommendation:
    """Used when Kev is unreachable, the breaker is open, or the data cannot be trusted."""
    rec = rule_baseline(state, source="fallback", reason=f"fallback:{why}")
    if rec.action != "MONITOR" and rec.route != "specialist_review":
        # Without a model opinion, anything beyond monitoring gets a human second look.
        return Recommendation(**{**rec.to_dict(), "reasons": rec.reasons + ("fallback_needs_review",),
                                 "route": "specialist_review"})
    return rec


def explain(state: Mapping[str, Any], action: str, urgency: str, reasons: list[str] | tuple[str, ...]) -> str:
    """Assemble an explanation from recorded facts only. Never invent a rationale."""
    t, p, r = state["telemetry"], state["production"], state["redundancy"]
    facts = [f"vibration {t['vibration_zone']} ({t['vibration_trend']})",
             f"bearing temperature {t['temperature_band']} ({t['temperature_trend']})",
             f"load {t['load_band']}"]
    if p["order_priority"] in ("HIGH", "CRITICAL"):
        facts.append(f"a {p['order_priority'].lower()}-priority order is running "
                     "(considered for planning, never overriding equipment protection)")
    facts.append(f"backup coverage {r['backup_coverage']}")
    if state["data_quality"]["status"] != "valid":
        facts.append(f"data quality {state['data_quality']['status']}")
    return f"{action} at {urgency} urgency because " + "; ".join(facts) + "."
