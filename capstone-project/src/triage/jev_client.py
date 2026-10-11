# capstone-project/src/triage/jev_client.py
"""Swappable decision client. The rest of the engine never imports a vendor SDK."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias

log = logging.getLogger("triage.jev_client")

JSONState: TypeAlias = str | Mapping[str, Any] | list[Any]
QuestionSpec: TypeAlias = Mapping[str, Any]          # {"type": "noul"|"choice"|"score", ...}
Questions: TypeAlias = Mapping[str, QuestionSpec]


# ---------- Normalised result types (vendor-neutral) ----------
@dataclass(frozen=True, slots=True)
class NoulResult:
    p_yes: float


@dataclass(frozen=True, slots=True)
class ChoiceResult:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float

    def margin(self) -> float:
        """Gap between the top two options: a useful diagnostic next to `confidence`."""
        top = sorted(self.probabilities.values(), reverse=True)
        return top[0] - (top[1] if len(top) > 1 else 0.0)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    score: float
    probabilities: Mapping[int, float]
    confidence: float
    levels: int

    def normalized(self) -> float:
        """Map 0..(levels-1) onto 0..1 so scores with different rubric lengths can be combined."""
        return self.score / (self.levels - 1) if self.levels > 1 else 0.0


Answer: TypeAlias = NoulResult | ChoiceResult | ScoreResult


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    answers: Mapping[str, Answer]
    model: str
    input_tokens: int | None
    latency_ms: float
    backend: str
    raw: Any = field(default=None, repr=False, compare=False)

    def noul(self, qid: str) -> NoulResult:
        a = self.answers[qid]
        if not isinstance(a, NoulResult):
            raise TypeError(f"{qid} is {type(a).__name__}, expected NoulResult")
        return a

    def choice(self, qid: str) -> ChoiceResult:
        a = self.answers[qid]
        if not isinstance(a, ChoiceResult):
            raise TypeError(f"{qid} is {type(a).__name__}, expected ChoiceResult")
        return a

    def score(self, qid: str) -> ScoreResult:
        a = self.answers[qid]
        if not isinstance(a, ScoreResult):
            raise TypeError(f"{qid} is {type(a).__name__}, expected ScoreResult")
        return a


class DecisionError(RuntimeError):
    """Raised when no typed decision could be obtained. Callers must fall back."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class DecisionClient(Protocol):
    backend: str

    def decide(self, state: JSONState, questions: Questions) -> DecisionResponse: ...


# ---------- Conversion from any SDK-shaped response ----------
def _normalise(raw_answers: Mapping[str, Any], questions: Questions) -> dict[str, Answer]:
    out: dict[str, Answer] = {}
    for qid, spec in questions.items():
        if qid not in raw_answers:
            raise DecisionError(f"answer missing for question {qid!r}", retryable=False)
        a = raw_answers[qid]
        kind = spec["type"]
        if kind == "noul":
            out[qid] = NoulResult(p_yes=float(a.noul))
        elif kind == "choice":
            out[qid] = ChoiceResult(
                choice=str(a.choice),
                probabilities={str(k): float(v) for k, v in a.probabilities.items()},
                confidence=float(a.confidence),
            )
        elif kind == "score":
            out[qid] = ScoreResult(
                score=float(a.score),
                probabilities={int(k): float(v) for k, v in a.probabilities.items()},
                confidence=float(a.confidence),
                levels=len(spec["criteria"]),
            )
        else:  # pragma: no cover - guarded by contract validation
            raise DecisionError(f"unknown question type {kind!r}", retryable=False)
    return out


# ---------- Backend 1: TypeSafe Jev ----------
class JevDecisionClient:
    backend = "jev"

    def __init__(self, model: str | None = None, timeout_s: float = 2.0, max_retries: int = 2,
                 *, base_url: str | None = None, api_key: str | None = None) -> None:
        from typesafe_sdk import RetryPolicy, TypeSafeClient  # lazy: optional dependency

        self._model = model or os.getenv("JEV_MODEL", "jev-latest")
        self._client = TypeSafeClient(
            model=self._model,
            api_key=api_key,            # None -> TYPESAFE_API_KEY
            base_url=base_url,          # None -> TYPESAFE_BASE_URL or the hosted API
            timeout=timeout_s,
            retry=RetryPolicy(max_retries=max_retries, timeout=timeout_s * (max_retries + 1)),
        )

    def decide(self, state: JSONState, questions: Questions) -> DecisionResponse:
        from typesafe_sdk import (
            TypeSafeAPIConnectionError,
            TypeSafeAPIError,
            TypeSafeAuthenticationError,
            TypeSafeError,
            TypeSafeUnprocessableEntityError,
        )

        t0 = time.perf_counter()
        try:
            r = self._client.system_one(state=state, questions=dict(questions))
        except (TypeSafeAuthenticationError, TypeSafeUnprocessableEntityError) as e:
            raise DecisionError(f"non-retryable Jev error: {e}", retryable=False) from e
        except (TypeSafeAPIConnectionError, TypeSafeAPIError) as e:
            # SDK already retried 408/429/5xx with backoff; surface to the circuit breaker.
            raise DecisionError(f"Jev unavailable after retries: {e}", retryable=True) from e
        except TypeSafeError as e:
            raise DecisionError(f"Jev client error: {e}", retryable=False) from e
        latency_ms = (time.perf_counter() - t0) * 1000
        resp = DecisionResponse(
            answers=_normalise(r.answers, questions),
            model=r.model,
            input_tokens=r.usage.input_tokens,
            latency_ms=latency_ms,
            backend=self.backend,
            raw=r,
        )
        log.info("decision backend=%s model=%s tokens=%s latency_ms=%.1f",
                 self.backend, resp.model, resp.input_tokens, latency_ms)
        return resp

    def close(self) -> None:
        self._client.close()


# ---------- Backend 1b: open-source Kev (same wire API, self-hosted) ----------
class KevDecisionClient(JevDecisionClient):
    """jaredpalmer/kev serves the System One API, so the TypeSafe SDK talks to it unchanged.

    Kev auth is optional: send KEV_API_KEY if the server sets one, otherwise any placeholder.
    Kev's confidence is NOT Jev's: calibrate thresholds separately for each backend.
    """

    backend = "kev"

    def __init__(self, model: str | None = None, timeout_s: float = 5.0, max_retries: int = 1,
                 *, base_url: str | None = None, api_key: str | None = None) -> None:
        super().__init__(
            model=model or os.getenv("KEV_MODEL", "kev-latest"),
            timeout_s=timeout_s,
            max_retries=max_retries,
            base_url=base_url or os.getenv("KEV_BASE_URL", "http://127.0.0.1:8009"),
            api_key=api_key or os.getenv("KEV_API_KEY") or "local",
        )


# ---------- Backend 2: System One Adapter (build before access) ----------
class AdapterDecisionClient:
    """Same interface backed by a general LLM. NOT calibrated like Jev: never tune thresholds on it."""

    backend = "adapter"

    def __init__(self, provider: str = "openai", model: str = "gpt-4o-mini") -> None:
        from system_one_adapter import SystemOneAdapterClient

        self._provider, self._model = provider, model
        self._client = SystemOneAdapterClient(
            structured_outputs=True,
            llm_answer_mode="probabilities",
            normalize_probabilities=True,
        )

    def decide(self, state: JSONState, questions: Questions) -> DecisionResponse:
        t0 = time.perf_counter()
        try:
            r = self._client.system_one(
                state=state, questions=dict(questions), provider=self._provider, model=self._model
            )
        except Exception as e:  # adapter wraps several provider SDKs
            raise DecisionError(f"adapter failure: {e}", retryable=True) from e
        return DecisionResponse(
            answers=_normalise(r.answers, questions),
            model=f"{self._provider}:{self._model}",
            input_tokens=None,
            latency_ms=(time.perf_counter() - t0) * 1000,
            backend=self.backend,
            raw=r,
        )


# ---------- Backend 3: offline fake for unit tests and demos ----------
FakeRule: TypeAlias = Callable[[JSONState, Questions], Mapping[str, Answer]]


class FakeDecisionClient:
    backend = "fake"

    def __init__(self, rule: FakeRule, latency_ms: float = 0.0, fail_times: int = 0) -> None:
        self._rule, self._latency_ms, self._fail_left = rule, latency_ms, fail_times

    def decide(self, state: JSONState, questions: Questions) -> DecisionResponse:
        if self._fail_left > 0:
            self._fail_left -= 1
            raise DecisionError("fake outage", retryable=True)
        if self._latency_ms:
            time.sleep(self._latency_ms / 1000)
        return DecisionResponse(
            answers=dict(self._rule(state, questions)),
            model="fake-0",
            input_tokens=None,
            latency_ms=self._latency_ms,
            backend=self.backend,
        )


def client_from_env() -> DecisionClient:
    backend = os.getenv("DECISION_BACKEND", "jev")
    if backend == "jev":
        return JevDecisionClient()
    if backend == "kev":
        return KevDecisionClient()
    if backend == "adapter":
        return AdapterDecisionClient(
            provider=os.getenv("ADAPTER_PROVIDER", "openai"),
            model=os.getenv("ADAPTER_MODEL", "gpt-4o-mini"),
        )
    raise ValueError(f"Unsupported DECISION_BACKEND={backend!r} (fake is for tests only)")
