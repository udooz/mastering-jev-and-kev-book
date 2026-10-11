from __future__ import annotations

from typing import Any

from triage.jev_client import ChoiceResult, FakeDecisionClient, NoulResult, ScoreResult
from triage.resilience import CircuitBreaker

from smartplant.contract import build_questions, eligible_actions
from smartplant.decision_service import SmartPlantDecisionService


def model(urgency: str, action: str, sev: float, std: float = 0.8, conf: float = 0.6, conflict: float = 0.1):
    def rule(state: Any, qs: Any):
        offered = list(qs["operational_action"]["criteria"])
        return {
            "intervention_urgency": ChoiceResult(urgency, {urgency: conf}, conf),
            "operational_action": ChoiceResult(action, {a: (conf if a == action else 0.0) for a in offered}, conf),
            "standard_workflow": NoulResult(std),
            "operational_severity": ScoreResult(sev, {}, 0.7, 5),
            "signals_conflict": NoulResult(conflict),
        }
    return rule


def test_transfer_load_not_offered_without_backup(state_for) -> None:
    st = state_for("S07")
    assert "TRANSFER_LOAD" not in eligible_actions(st)
    assert "TRANSFER_LOAD" not in build_questions(st)["operational_action"]["criteria"]


def test_model_cannot_go_below_safety_floor(state_for) -> None:
    svc = SmartPlantDecisionService(FakeDecisionClient(model("ROUTINE", "MONITOR", 0.5)))
    r = svc.evaluate(state_for("S06"))                      # vibration in danger zone
    assert r.urgency == "URGENT" and r.action == "INSPECT"
    assert any("floor_raised" in x for x in r.reasons)


def test_production_priority_never_suppresses_inspection(state_for) -> None:
    svc = SmartPlantDecisionService(FakeDecisionClient(model("ROUTINE", "MONITOR", 1.0)))
    r = svc.evaluate(state_for("S05"))                      # CRITICAL order, alert-zone vibration
    assert r.action != "MONITOR"


def test_transfer_requires_approval(state_for) -> None:
    svc = SmartPlantDecisionService(FakeDecisionClient(model("URGENT", "TRANSFER_LOAD", 3.0)))
    r = svc.evaluate(state_for("S06"))
    assert r.action == "TRANSFER_LOAD" and r.requires_approval


def test_conflict_and_low_confidence_go_to_specialist(state_for) -> None:
    svc = SmartPlantDecisionService(FakeDecisionClient(model("PRIORITY", "INSPECT", 2.0, conflict=0.9)))
    assert svc.evaluate(state_for("S09")).route == "specialist_review"
    svc = SmartPlantDecisionService(FakeDecisionClient(model("PRIORITY", "INSPECT", 2.0, conf=0.1)))
    assert "low_confidence" in svc.evaluate(state_for("S02")).reasons


def test_invalid_data_never_reaches_model(state_for) -> None:
    calls = []
    svc = SmartPlantDecisionService(FakeDecisionClient(lambda s, q: calls.append(1) or {}))
    r = svc.evaluate(state_for("S04"))
    assert not calls and r.source == "fallback" and r.route == "specialist_review"


def test_outage_falls_back_and_opens_breaker(state_for) -> None:
    br = CircuitBreaker(failure_threshold=2, cooldown_s=60)
    svc = SmartPlantDecisionService(FakeDecisionClient(model("PRIORITY", "INSPECT", 2.0), fail_times=5), br)
    st = state_for("S02")
    for _ in range(3):
        r = svc.evaluate(st)
        assert r.source == "fallback" and r.action == "INSPECT"
    assert br.state.value == "open"


def test_invalid_label_from_backend_falls_back(state_for) -> None:
    svc = SmartPlantDecisionService(FakeDecisionClient(model("PRIORITY", "REBOOT_PLC", 2.0)))
    r = svc.evaluate(state_for("S02"))
    assert r.source == "fallback" and "fallback:invalid_response" in r.reasons
