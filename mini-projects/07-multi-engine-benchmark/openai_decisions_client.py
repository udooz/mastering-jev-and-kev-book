# mini-projects/07-multi-engine-benchmark/openai_decisions_client.py
"""DecisionClient for the OpenAI Decisions API (POST /v1/decisions, model gpt-6-luna; public beta).

Maps the book's dict contract (noul / choice / score) onto the Decisions questions array
(predicate / choice / score) and maps the answers back to NoulResult / ChoiceResult / ScoreResult,
so the rest of the engine never sees the vendor schema.

  uv run python mini-projects/07-multi-engine-benchmark/openai_decisions_client.py --dry-run
  uv sync --group ch7 && export OPENAI_API_KEY=sk-...
  uv run python mini-projects/07-multi-engine-benchmark/openai_decisions_client.py
Used as an engine by multi_engine_eval.py (--engines openai-decisions).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from triage.jev_client import (Answer, ChoiceResult, DecisionError, DecisionResponse, JSONState,
                               NoulResult, Questions, ScoreResult)

DEFAULT_MODEL = "gpt-6-luna"
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


# ---------- Request mapping: book contract -> Decisions questions array ----------
def to_decision_questions(questions: Questions) -> tuple[list[dict[str, Any]], dict[str, Answer]]:
    """Return (questions to send, answers settled locally).

    A Choice with a single eligible option (e.g. refund_pipeline pruned away) is not a question:
    the API needs 2-255 choices, and asking would only add latency, so we answer it here.
    """
    out: list[dict[str, Any]] = []
    local: dict[str, Answer] = {}
    for qid, q in questions.items():
        kind, text = q["type"], str(q.get("instructions", ""))
        if kind == "noul":
            tf = q.get("criteria") or {}
            if tf:
                text += f"\nTrue when: {tf.get('true', '')}\nFalse when: {tf.get('false', '')}"
            out.append({"type": "predicate", "name": qid, "instructions": text})
        elif kind == "choice":
            crit: Mapping[str, Any] = q["criteria"]
            if len(crit) == 1:
                only = next(iter(crit))
                local[qid] = ChoiceResult(only, {only: 1.0}, 1.0)
                continue
            if len(crit) > 255:
                raise DecisionError(f"{qid}: {len(crit)} choices exceeds the API limit of 255", retryable=False)
            out.append({"type": "choice", "name": qid, "instructions": text,
                        "choices": [{"value": k, "description": str(v or k)} for k, v in crit.items()]})
        elif kind == "score":
            levels = [_level_text(x) for x in q["criteria"]]
            if not 2 <= len(levels) <= 10:
                raise DecisionError(f"{qid}: score needs 2-10 levels, got {len(levels)}", retryable=False)
            out.append({"type": "score", "name": qid, "instructions": text,
                        "levels": [{"label": str(i), "description": d} for i, d in enumerate(levels)]})
        else:
            raise DecisionError(f"{qid}: unknown question type {kind!r}", retryable=False)
    return out, local


def _level_text(level: Any) -> str:
    """Score levels are strings or {"what": ..., "examples": [...]} rubric rows (Chapter 2)."""
    if isinstance(level, Mapping):
        ex = "; ".join(map(str, level.get("examples", [])))
        return f"{level.get('what', '')}" + (f" (e.g. {ex})" if ex else "")
    return str(level)


def to_input(state: JSONState) -> str:
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, default=str)


# ---------- Response mapping: Decisions answers array -> book result types ----------
def from_decision_answers(answers: Sequence[Mapping[str, Any]], sent: Sequence[Mapping[str, Any]],
                          questions: Questions) -> dict[str, Answer]:
    """Answers come back in question order; match by name and fall back to position."""
    by_name = {a.get("name"): a for a in answers if a.get("name")}
    out: dict[str, Answer] = {}
    for i, q in enumerate(sent):
        qid = q["name"]
        a = by_name.get(qid) or (answers[i] if i < len(answers) else None)
        if a is None:
            raise DecisionError(f"answer missing for question {qid!r}", retryable=False)
        kind = a.get("type")
        if kind == "refusal":                 # the host declined this question: policy must fall back
            raise DecisionError(f"{qid}: refused by the Decisions API", retryable=False)
        if kind != q["type"]:
            raise DecisionError(f"{qid}: expected {q['type']}, got {kind!r}", retryable=False)
        if kind == "predicate":
            out[qid] = NoulResult(float(a["probability"]))
        elif kind == "choice":
            probs = {str(p["value"]): float(p["probability"]) for p in a["probabilities"]}
            choice = str(a["choice"])
            if choice not in questions[qid]["criteria"]:
                raise DecisionError(f"{qid}: label outside contract: {choice!r}", retryable=False)
            out[qid] = ChoiceResult(choice, probs, float(a["confidence"]))
        else:  # score: `score` is the expected zero-based level index, like Jev's
            probs_i = {int(p["value"]): float(p["probability"]) for p in a["probabilities"]}
            out[qid] = ScoreResult(float(a["score"]), probs_i, float(a["confidence"]),
                                   len(questions[qid]["criteria"]))
    return out


# ---------- The client ----------
class OpenAIDecisionsClient:
    """Its confidence is not Jev's: tune thresholds on this backend separately (Chapter 4)."""

    backend = "openai-decisions"

    def __init__(self, model: str | None = None, timeout_s: float = 3.0, max_retries: int = 2,
                 sdk_client: Any | None = None) -> None:
        self._model = model or os.getenv("OPENAI_DECISIONS_MODEL", DEFAULT_MODEL)
        if sdk_client is None:
            try:
                from openai import OpenAI          # openai>=3.26 (uv sync --group ch7)
            except ImportError as e:
                raise RuntimeError("openai SDK missing: run `uv sync --group ch7`") from e
            # Reads OPENAI_API_KEY; OPENAI_BASE_URL can point at a gateway that serves the same API.
            try:
                sdk_client = OpenAI(timeout=timeout_s, max_retries=max_retries)
            except Exception as e:                 # e.g. OpenAIError: no OPENAI_API_KEY
                raise RuntimeError(f"cannot create OpenAI client: {e}") from e
        self._sdk = sdk_client

    def decide(self, state: JSONState, questions: Questions) -> DecisionResponse:
        sent, local = to_decision_questions(questions)
        t0 = time.perf_counter()
        if not sent:                               # everything was settled locally
            return DecisionResponse(local, self._model, 0, 0.0, self.backend)
        try:
            resp = self._sdk.decisions.create(model=self._model, input=to_input(state), questions=sent)
        except Exception as e:                     # map SDK errors without importing them at module level
            raise _as_decision_error(e) from e
        latency_ms = (time.perf_counter() - t0) * 1000
        body = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        answers = {**local, **from_decision_answers(body["answers"], sent, questions)}
        usage = body.get("usage") or {}
        return DecisionResponse(
            answers={qid: answers[qid] for qid in questions},     # keep the contract's order
            model=str(body.get("model", self._model)),
            input_tokens=usage.get("input_tokens"),
            latency_ms=latency_ms,
            backend=self.backend,
            raw=body,
        )


def _as_decision_error(e: Exception) -> DecisionError:
    status = getattr(e, "status_code", None)
    name = type(e).__name__
    if status is not None:
        return DecisionError(f"openai-decisions HTTP {status}: {e}", retryable=status in RETRYABLE_STATUS)
    if name in ("APIConnectionError", "APITimeoutError"):
        return DecisionError(f"openai-decisions transport: {e}", retryable=True)
    return DecisionError(f"openai-decisions client error: {name}: {e}", retryable=False)


def main(argv: list[str] | None = None) -> int:
    from triage.state_builder import build_questions, build_state, parse_report

    ap = argparse.ArgumentParser(description="Send one triage request to the OpenAI Decisions API")
    ap.add_argument("--text", default="Got OTPs I never asked for and 2 payments I didn't make just now")
    ap.add_argument("--model", default=None, help=f"default: $OPENAI_DECISIONS_MODEL or {DEFAULT_MODEL}")
    ap.add_argument("--dry-run", action="store_true", help="print the request body; send nothing")
    args = ap.parse_args(argv)

    report = parse_report({"report_id": "smoke-1", "body": args.text})
    state, questions = build_state(report), build_questions(report, speculative=False)
    if args.dry_run:
        sent, local = to_decision_questions(questions)
        model = args.model or os.getenv("OPENAI_DECISIONS_MODEL", DEFAULT_MODEL)
        print(json.dumps({"model": model, "input": to_input(state), "questions": sent}, indent=2))
        if local:
            print(f"\nanswered locally (single option): {sorted(local)}", file=sys.stderr)
        return 0
    if not os.getenv("OPENAI_API_KEY"):
        print("set OPENAI_API_KEY first (or use --dry-run)", file=sys.stderr)
        return 2
    try:
        r = OpenAIDecisionsClient(model=args.model).decide(state, questions)
    except (RuntimeError, DecisionError) as e:
        print(f"decision failed: {e}", file=sys.stderr)
        return 1
    for qid, a in r.answers.items():
        print(f"{qid:20} {a}")
    print(f"\n{r.model}: {r.latency_ms:.0f} ms, {r.input_tokens} input tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
