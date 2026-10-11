# capstone-project/src/triage/resilience.py
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Final, Mapping

from triage.jev_client import (ChoiceResult, DecisionClient, DecisionError, DecisionResponse,
                               NoulResult, ScoreResult)

log = logging.getLogger("triage.resilience")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._threshold, self._cooldown, self._clock = failure_threshold, cooldown_s, clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state_unlocked()

    def _state_unlocked(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self._cooldown:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow(self) -> bool:
        with self._lock:
            s = self._state_unlocked()
            if s is BreakerState.CLOSED:
                return True
            if s is BreakerState.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures, self._opened_at, self._probe_in_flight = 0, None, False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._probe_in_flight or self._failures >= self._threshold:
                self._opened_at = self._clock()
                self._probe_in_flight = False
                log.warning("circuit OPEN after %d failures", self._failures)


# ---- Deterministic fallback: conservative, explainable, no model involved ----
_FRAUD_HINTS: Final = re.compile(
    r"\b(not me|didn'?t (make|authori[sz]e)|unauthori[sz]ed|stolen|hack(ed)?|otp|phishing|"
    r"someone (used|logged)|fraud|scam)\b", re.I)


def fallback_response(state: Mapping[str, Any]) -> DecisionResponse:
    body = str(state.get("report", {}).get("body", ""))
    fraud = bool(_FRAUD_HINTS.search(body)) or bool(
        state.get("computed_signals", {}).get("new_device_login")
        and state.get("computed_signals", {}).get("location_differs_from_usual"))
    target = "secops_agent" if fraud else "manual_escalation"
    probs = {k: 0.0 for k in ("tier1_support", "secops_agent", "refund_pipeline", "manual_escalation")}
    probs[target] = 1.0
    return DecisionResponse(
        answers={
            # p=1.0 on secops forces the fraud override in the policy engine; 0.0 otherwise.
            "is_fraud_suspected": NoulResult(1.0 if fraud else 0.0),
            "urgency_rating": ScoreResult(4.0 if fraud else 2.0, {}, 0.0, 5),
            # confidence 0.0 means the non-fraud path always lands in manual_escalation.
            "dispatch_target": ChoiceResult(target, probs, 0.0),
        },
        model="fallback-rules-1", input_tokens=0, latency_ms=0.0, backend="fallback",
    )


@dataclass(slots=True)
class ResilientDecider:
    client: DecisionClient
    breaker: CircuitBreaker

    def decide(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> DecisionResponse:
        if not self.breaker.allow():
            return fallback_response(state)
        try:
            resp = self.client.decide(state, questions)
        except DecisionError as e:
            if e.retryable:
                self.breaker.record_failure()
            else:
                # Contract/auth bugs are OUR fault: alert loudly, don't mask with the breaker.
                log.error("non-retryable decision error: %s", e)
            return fallback_response(state)
        self.breaker.record_success()
        return resp
