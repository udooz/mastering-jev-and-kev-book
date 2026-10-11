# capstone-project/src/triage/engine.py
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from triage.executors import Executor, Receipt
from triage.jev_client import DecisionResponse
from triage.policy_engine import DEFAULT_THRESHOLDS, PolicyDecision, Thresholds, decide
from triage.state_builder import InvalidReport, build_questions, build_state, parse_report
from triage.trace import DecisionTrace
from triage.verifier import FreshStateVerifier, Verification

log = logging.getLogger("triage.engine")


class Decider(Protocol):  # ResilientDecider (optionally wrapping a CascadeDecider)
    def decide(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> DecisionResponse: ...


@dataclass(frozen=True, slots=True)
class TriageResult:
    report_id: str
    destination: str
    priority: str
    urgency: int
    band: str
    verified: bool
    artifact_id: str | None
    backend: str
    decision_ms: float
    total_ms: float
    reasons: tuple[str, ...]


@dataclass(slots=True)
class Metrics:
    counters: dict[str, int] = field(default_factory=dict)
    timings_ms: dict[str, list[float]] = field(default_factory=dict)

    def inc(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    def observe(self, name: str, ms: float) -> None:
        self.timings_ms.setdefault(name, []).append(ms)


@dataclass(slots=True)
class TriageEngine:
    decider: Decider
    executors: Mapping[str, Executor]
    verifier: FreshStateVerifier
    trace_path: Path
    thresholds: Thresholds = DEFAULT_THRESHOLDS
    verify_retry_delay_s: float = 2.0
    sleep: Callable[[float], None] = time.sleep
    metrics: Metrics = field(default_factory=Metrics)

    def handle(self, payload: Mapping[str, Any]) -> TriageResult:
        t0 = time.perf_counter()
        try:
            report = parse_report(payload)
        except InvalidReport:
            self.metrics.inc("invalid_report")
            raise
        state = build_state(report)
        questions = build_questions(report)

        resp = self.decider.decide(state, questions)
        self.metrics.inc(f"backend.{resp.backend}")
        self.metrics.observe("decision_ms", resp.latency_ms)

        decision = decide(resp, state, self.thresholds)
        self.metrics.inc(f"route.{decision.destination}.{decision.band}")

        receipt, verification = self._execute_and_verify(report.report_id, state, decision)
        if not verification.ok and decision.destination != "manual_escalation":
            # Two failed checks -> hand to a human with both receipts. Recovery is code, not judgment.
            self.metrics.inc("verification_failed")
            escalation = PolicyDecision("manual_escalation", "low", decision.priority, decision.urgency,
                                        False, True, decision.reasons + (f"verify:{verification.detail}",))
            receipt, verification = self._execute_and_verify(report.report_id, state, escalation)
            decision = escalation

        trace = DecisionTrace.build(report.report_id, state, questions, resp, decision)
        trace.executor_receipt = None if receipt is None else asdict(receipt)
        trace.verification = {"outcome": verification.outcome, "detail": verification.detail}
        trace.append_jsonl(self.trace_path)

        total_ms = (time.perf_counter() - t0) * 1000
        self.metrics.observe("total_ms", total_ms)
        return TriageResult(report.report_id, decision.destination, decision.priority, decision.urgency,
                            decision.band, verification.ok, receipt.artifact_id if receipt else None,
                            resp.backend, resp.latency_ms, total_ms, decision.reasons)

    def _execute_and_verify(self, report_id: str, state: Mapping[str, Any],
                            d: PolicyDecision) -> tuple[Receipt | None, Verification]:
        executor = self.executors[d.destination]
        try:
            receipt = executor.execute(report_id, state, d)
        except Exception as e:  # executor boundary: never lose the report
            log.exception("executor %s failed", d.destination)
            self.metrics.inc(f"executor_error.{d.destination}")
            return None, Verification("not_found", f"executor error: {type(e).__name__}")
        v = self.verifier.verify(receipt, d)
        if not v.ok:
            self.sleep(self.verify_retry_delay_s)      # eventual consistency: one fresh re-read
            v = self.verifier.verify(receipt, d)
        return receipt, v
