"""Industrial evaluation metrics for the three-way benchmark (rules vs LLM vs Kev)."""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from smartplant.contract import ACTIONS, URGENCIES
from smartplant.scenario_runner import ScenarioResult

HIGH_URGENCY = {"URGENT", "CRITICAL"}
DISRUPTIVE = {"SCHEDULE_MAINTENANCE", "TRANSFER_LOAD", "REQUEST_SHUTDOWN"}


@dataclass(frozen=True, slots=True)
class EngineMetrics:
    engine: str
    n: int
    agreement: float                    # action AND urgency within expert-accepted sets
    critical_miss_rate: float           # critical cases not escalated to URGENT/CRITICAL (lower is better)
    unnecessary_intervention_rate: float  # healthy cases that got a disruptive action (lower is better)
    response_validity: float            # labels inside the contract + action was eligible
    escalation_quality: float           # expected-specialist cases that reached a specialist
    fallback_success: float | None      # service-down cases handled safely (None = not applicable)
    audit_completeness: float
    p50_ms: float
    p95_ms: float
    cost_per_state_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rate(num: int, den: int, empty: float = 1.0) -> float:
    return round(num / den, 3) if den else empty


def _rate_or_none(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def compute(results: Sequence[ScenarioResult], scenarios: Sequence[Mapping[str, Any]],
            usd_per_state: Mapping[str, float] | None = None) -> list[EngineMetrics]:
    """usd_per_state: engine -> cost per decision (e.g. Kev GPU-hour / throughput, LLM tokens)."""
    spec = {s["id"]: s for s in scenarios}
    by_engine: dict[str, list[ScenarioResult]] = defaultdict(list)
    for r in results:
        by_engine[r.engine].append(r)
    out: list[EngineMetrics] = []
    for engine, rows in by_engine.items():
        agree = crit_n = crit_miss = healthy_n = unnecessary = valid = 0
        spec_n = spec_hit = down_n = down_ok = audit_ok = 0
        lat: list[float] = []
        for r in rows:
            exp, rec = spec[r.scenario_id]["expected"], r.recommendation
            agree += rec.action in exp["actions"] and rec.urgency in exp["urgencies"]
            if exp["critical"]:
                crit_n += 1
                crit_miss += rec.urgency not in HIGH_URGENCY
            if not exp["intervention"]:
                healthy_n += 1
                unnecessary += rec.action in DISRUPTIVE
            valid += rec.action in ACTIONS and rec.urgency in URGENCIES and 1 <= rec.severity <= 5
            if exp.get("route") == "specialist_review":
                spec_n += 1
                spec_hit += rec.route == "specialist_review"
            if spec[r.scenario_id].get("decision_service_down") and engine != "rules":
                down_n += 1
                down_ok += rec.source == "fallback" and rec.action != "MONITOR"
            audit_ok += r.audit_complete
            lat.append(r.wall_ms)
        q = statistics.quantiles(lat, n=20) if len(lat) >= 2 else lat * 19
        out.append(EngineMetrics(
            engine=engine, n=len(rows),
            agreement=_rate(agree, len(rows)),
            critical_miss_rate=_rate(crit_miss, crit_n, 0.0),
            unnecessary_intervention_rate=_rate(unnecessary, healthy_n, 0.0),
            response_validity=_rate(valid, len(rows)),
            escalation_quality=_rate(spec_hit, spec_n),
            fallback_success=_rate_or_none(down_ok, down_n),
            audit_completeness=_rate(audit_ok, len(rows)),
            p50_ms=round(statistics.median(lat), 1), p95_ms=round(q[18], 1),
            cost_per_state_usd=(usd_per_state or {}).get(engine, 0.0),
        ))
    return out


def kev_cost_per_state(gpu_usd_per_hour: float, decisions_per_second: float) -> float:
    """Self-hosted cost: you pay for the box, not the token. e.g. $2.50/h at 100 req/s."""
    if decisions_per_second <= 0:
        raise ValueError("throughput must be positive")
    return gpu_usd_per_hour / 3600 / decisions_per_second
