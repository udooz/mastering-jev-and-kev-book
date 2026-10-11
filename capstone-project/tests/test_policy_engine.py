# capstone-project/tests/test_policy_engine.py
from triage.jev_client import ChoiceResult, DecisionResponse, NoulResult, ScoreResult
from triage.policy_engine import decide


def resp(fraud: float, urgency_raw: float, target: str, conf: float, manip: float = 0.0) -> DecisionResponse:
    probs = {k: 0.0 for k in ("tier1_support", "secops_agent", "refund_pipeline", "manual_escalation")}
    probs[target] = conf
    return DecisionResponse(
        answers={
            "is_fraud_suspected": NoulResult(fraud),
            "urgency_rating": ScoreResult(urgency_raw, {}, 0.9, 5),
            "dispatch_target": ChoiceResult(target, probs, conf),
            "contains_instructions_to_system": NoulResult(manip),
        },
        model="fake-0", input_tokens=None, latency_ms=0.0, backend="fake",
    )


def test_fraud_overrides_model_route() -> None:
    d = decide(resp(0.93, 3.8, "tier1_support", 0.9), {})
    assert d.destination == "secops_agent" and d.priority == "P1"


def test_low_confidence_refund_escalates() -> None:
    d = decide(resp(0.05, 2.0, "refund_pipeline", 0.72), {})
    assert d.destination == "manual_escalation" and d.requires_human_approval


def test_big_refund_needs_approval_even_when_confident() -> None:
    d = decide(resp(0.02, 2.9, "refund_pipeline", 0.97),
               {"computed_signals": {"disputed_amount_band": "above_50k"}})
    assert d.destination == "refund_pipeline" and d.requires_human_approval


def test_manipulation_never_auto_routes() -> None:
    d = decide(resp(0.0, 1.0, "refund_pipeline", 0.99, manip=0.8), {})
    assert d.destination == "manual_escalation"
