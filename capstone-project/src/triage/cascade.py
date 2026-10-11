# capstone-project/src/triage/cascade.py
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from triage.jev_client import DecisionClient, DecisionError, DecisionResponse


@dataclass(slots=True)
class CascadeDecider:
    fast: DecisionClient          # Jev
    strong: DecisionClient        # AdapterDecisionClient(frontier model)
    escalate_below: float = 0.70  # dispatch_target confidence
    fraud_grey_zone: tuple[float, float] = (0.30, 0.85)
    backend: str = "cascade"      # satisfies the DecisionClient protocol

    def decide(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> DecisionResponse:
        first = self.fast.decide(state, questions)
        conf = first.choice("dispatch_target").confidence
        fraud = first.noul("is_fraud_suspected").p_yes
        lo, hi = self.fraud_grey_zone
        if conf >= self.escalate_below and not (lo <= fraud < hi):
            return first
        try:
            second = self.strong.decide(state, questions)
        except DecisionError:
            return first                      # keep the calibrated answer; policy will gate it
        # LLM probabilities are not calibrated: cap confidence so policy lands in the medium band.
        dt = second.choice("dispatch_target")
        capped = replace(dt, confidence=min(dt.confidence, 0.84))
        answers = {**second.answers, "dispatch_target": capped}
        return replace(second, answers=answers, latency_ms=first.latency_ms + second.latency_ms,
                       backend=f"cascade({first.backend}->{second.backend})")
