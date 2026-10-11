# capstone-project/tests/test_resilience.py
from triage.jev_client import FakeDecisionClient, NoulResult
from triage.resilience import BreakerState, CircuitBreaker, ResilientDecider


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_breaker_opens_then_recovers() -> None:
    clock = Clock()
    ok = FakeDecisionClient(lambda s, q: {"is_fraud_suspected": NoulResult(0.1)}, fail_times=3)
    d = ResilientDecider(ok, CircuitBreaker(failure_threshold=3, cooldown_s=10, clock=clock))
    state = {"report": {"body": "someone used my card, not me"}}
    for _ in range(3):
        assert d.decide(state, {}).backend == "fallback"
    assert d.breaker.state is BreakerState.OPEN
    clock.t = 11
    assert d.decide(state, {}).backend == "fake"          # half-open probe succeeds
    assert d.breaker.state is BreakerState.CLOSED


def test_fallback_sends_fraud_to_secops() -> None:
    from triage.resilience import fallback_response
    r = fallback_response({"report": {"body": "I didn't authorise this payment"}})
    assert r.choice("dispatch_target").choice == "secops_agent"
