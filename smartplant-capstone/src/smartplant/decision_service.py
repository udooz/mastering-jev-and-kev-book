"""SmartPlant decision service: state -> Kev (via the shared triage client) -> policy -> Recommendation.

Reuses Part I unchanged: `triage.jev_client` (DecisionClient, KevDecisionClient, FakeDecisionClient)
and `triage.resilience.CircuitBreaker`. Only the contract and the policy are domain-specific.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from triage.jev_client import DecisionClient, DecisionError, DecisionResponse
from triage.resilience import CircuitBreaker

from smartplant.contract import CONTRACT_VERSION, build_questions, to_severity_1_5
from smartplant.fallback_rules import (ACTION_RANK, URGENCY_RANK, explain, fallback_recommendation,
                                       rule_baseline, safety_floor)
from smartplant.models import Recommendation

log = logging.getLogger("smartplant.decision_service")
POLICY_VERSION = "smartplant-policy/1.0.0"


@dataclass(frozen=True, slots=True)
class KevThresholds:
    """Starting points for Kev-4B. Kev's confidence is not Jev's: recalibrate per model and per plant."""
    urgency_min_conf: float = 0.25
    action_min_conf: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({
        "MONITOR": 0.30, "INSPECT": 0.20, "SCHEDULE_MAINTENANCE": 0.30,
        "TRANSFER_LOAD": 0.45, "REQUEST_SHUTDOWN": 0.0,
    }))
    standard_workflow_min: float = 0.60
    conflict_escalate: float = 0.50


@dataclass(slots=True)
class SmartPlantDecisionService:
    client: DecisionClient
    breaker: CircuitBreaker = field(default_factory=lambda: CircuitBreaker(failure_threshold=3, cooldown_s=30))
    thresholds: KevThresholds = field(default_factory=KevThresholds)

    def evaluate(self, state: Mapping[str, Any]) -> Recommendation:
        # 1. Safety partition: protective limits are the PLC's job. Mirror them; never wait on a model.
        if state["protective_limit_reached"]:
            return rule_baseline(state, source="safety_rules", reason="protective_limit: model not consulted")
        # 2. Untrusted evidence never reaches the model.
        if state["data_quality"]["status"] == "invalid":
            return fallback_recommendation(state, "invalid_data")
        # 3. Fail fast when the decision service is known to be down.
        if not self.breaker.allow():
            return fallback_recommendation(state, "breaker_open")
        questions = build_questions(state)
        try:
            resp = self.client.decide(state, questions)
        except DecisionError as e:
            if e.retryable:
                self.breaker.record_failure()
            else:
                log.error("non-retryable decision error (contract/auth bug?): %s", e)
            return fallback_recommendation(state, "decision_service_unavailable")
        self.breaker.record_success()
        try:
            return self._apply_policy(state, questions, resp)
        except (KeyError, TypeError, ValueError) as e:     # invalid/missing answer from any backend
            log.warning("invalid decision response: %s", e)
            return fallback_recommendation(state, "invalid_response")

    def _apply_policy(self, state: Mapping[str, Any], questions: Mapping[str, Any],
                      resp: DecisionResponse) -> Recommendation:
        th = self.thresholds
        u, a = resp.choice("intervention_urgency"), resp.choice("operational_action")
        std = resp.noul("standard_workflow").p_yes
        severity = to_severity_1_5(resp.score("operational_severity").score)
        conflict = resp.noul("signals_conflict").p_yes if "signals_conflict" in resp.answers else 0.0
        if a.choice not in questions["operational_action"]["criteria"]:
            raise ValueError(f"action {a.choice!r} not offered")          # adapters/LLMs can do this
        if u.choice not in URGENCY_RANK:
            raise ValueError(f"urgency {u.choice!r} not in contract")

        urgency, action = u.choice, a.choice
        reasons = [f"{resp.backend}: urgency={u.choice}@{u.confidence:.2f}",
                   f"action={a.choice}@{a.confidence:.2f}", f"severity={severity}",
                   f"standard_workflow p={std:.2f}"]

        # Code-computed safety floor: a model may escalate past it, never below it.
        floor = safety_floor(state)
        if floor and URGENCY_RANK[urgency] < URGENCY_RANK[floor[0]]:
            urgency = floor[0]
            reasons.append(f"floor_raised_urgency->{urgency}")
        if floor and ACTION_RANK[action] < ACTION_RANK[floor[1]]:
            action = floor[1]
            reasons.append(f"floor_raised_action->{action}")
        if severity >= 4 and action == "MONITOR":
            action = "INSPECT"
            reasons.append("severity>=4 never just monitors")

        route = "standard_cmms" if std >= th.standard_workflow_min else "specialist_review"
        if conflict >= th.conflict_escalate:
            route = "specialist_review"
            reasons.append(f"signals_conflict p={conflict:.2f}")
        if state["data_quality"]["status"] == "degraded":
            route = "specialist_review"
            reasons.append("degraded_data")
        if u.confidence < th.urgency_min_conf or a.confidence < th.action_min_conf.get(action, 0.5):
            route = "specialist_review"
            reasons.append("low_confidence")
        if urgency == "CRITICAL":
            route = "specialist_review"

        return Recommendation(
            asset_id=state["asset"]["id"], urgency=urgency, action=action, severity=severity,  # type: ignore[arg-type]
            route=route,  # type: ignore[arg-type]
            requires_approval=action in ("TRANSFER_LOAD", "REQUEST_SHUTDOWN"),
            source=resp.backend, reasons=tuple(reasons),
            explanation=explain(state, action, urgency, reasons),
            snapshot_hash=state["snapshot_hash"], contract_version=CONTRACT_VERSION,
            model=resp.model, latency_ms=round(resp.latency_ms, 1),
            confidence={"urgency": round(u.confidence, 3), "action": round(a.confidence, 3),
                        "standard_workflow": round(std, 3), "signals_conflict": round(conflict, 3)},
        )
