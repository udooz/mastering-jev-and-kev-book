# mini-projects/07-multi-engine-benchmark/test_openai_decisions_client.py
"""Offline tests for the Decisions mapping: a fake SDK returns the documented response shape.

  uv run pytest mini-projects/07-multi-engine-benchmark
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openai_decisions_client import OpenAIDecisionsClient, to_decision_questions
from triage.jev_client import DecisionError
from triage.state_builder import build_questions, build_state, parse_report


class FakeSDK:
    """Mimics `OpenAI().decisions.create(...)` and records the request."""

    def __init__(self, answers: list[dict[str, Any]] | None = None, exc: Exception | None = None) -> None:
        self.sent: dict[str, Any] = {}
        self._answers, self._exc = answers, exc
        self.decisions = SimpleNamespace(create=self._create)

    def _create(self, **kw: Any) -> dict[str, Any]:
        self.sent = kw
        if self._exc:
            raise self._exc
        return {"model": "gpt-6-luna-2026-10-01", "usage": {"input_tokens": 412, "output_tokens": 0},
                "answers": self._answers or _good_answers(kw["questions"])}


def _good_answers(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for q in questions:
        if q["type"] == "predicate":
            out.append({"type": "predicate", "name": q["name"], "probability": 0.93})
        elif q["type"] == "choice":
            vals = [c["value"] for c in q["choices"]]
            probs = [{"value": v, "probability": 0.7 if v == "secops_agent" else 0.3 / (len(vals) - 1)} for v in vals]
            out.append({"type": "choice", "name": q["name"], "choice": "secops_agent",
                        "probabilities": probs, "confidence": 0.62})
        else:
            n = len(q["levels"])
            out.append({"type": "score", "name": q["name"], "score": 3.2, "confidence": 0.55,
                        "probabilities": [{"value": i, "label": str(i), "probability": 1 / n} for i in range(n)]})
    return out


def _contract(body: str = "Got OTPs I never asked for and 2 payments I didn't make just now"):
    r = parse_report({"report_id": "t-1", "body": body})
    return build_state(r), build_questions(r, speculative=False)


def test_request_uses_decisions_schema() -> None:
    _, qs = _contract()
    sent, local = to_decision_questions(qs)
    assert not local
    kinds = {q["name"]: q["type"] for q in sent}
    assert kinds == {"is_fraud_suspected": "predicate", "urgency_rating": "score", "dispatch_target": "choice"}
    choice = next(q for q in sent if q["type"] == "choice")
    assert {c["value"] for c in choice["choices"]} == set(qs["dispatch_target"]["criteria"])


def test_answers_map_back_to_book_types() -> None:
    state, qs = _contract()
    sdk = FakeSDK()
    r = OpenAIDecisionsClient(sdk_client=sdk).decide(state, qs)
    assert isinstance(sdk.sent["input"], str) and sdk.sent["model"] == "gpt-6-luna"
    assert r.noul("is_fraud_suspected").p_yes == pytest.approx(0.93)
    assert r.choice("dispatch_target").choice == "secops_agent"
    assert sum(r.choice("dispatch_target").probabilities.values()) == pytest.approx(1.0)
    assert r.score("urgency_rating").levels == len(qs["urgency_rating"]["criteria"])
    assert r.input_tokens == 412 and r.backend == "openai-decisions"
    assert list(r.answers) == list(qs)


def test_refusal_becomes_decision_error() -> None:
    state, qs = _contract()
    answers = _good_answers(to_decision_questions(qs)[0])
    answers[-1] = {"type": "refusal", "name": answers[-1]["name"]}
    with pytest.raises(DecisionError) as ei:
        OpenAIDecisionsClient(sdk_client=FakeSDK(answers)).decide(state, qs)
    assert not ei.value.retryable


def test_rate_limit_is_retryable() -> None:
    class RateLimitError(Exception):
        status_code = 429

    state, qs = _contract()
    with pytest.raises(DecisionError) as ei:
        OpenAIDecisionsClient(sdk_client=FakeSDK(exc=RateLimitError("slow down"))).decide(state, qs)
    assert ei.value.retryable


def test_label_outside_contract_is_rejected() -> None:
    state, qs = _contract()
    answers = _good_answers(to_decision_questions(qs)[0])
    for a in answers:
        if a["type"] == "choice":
            a["choice"] = "approve_refund_now"
    with pytest.raises(DecisionError):
        OpenAIDecisionsClient(sdk_client=FakeSDK(answers)).decide(state, qs)
