# capstone-project/src/triage/trace.py
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from triage.contract import CONTRACT_VERSION, contract_hash
from triage.jev_client import ChoiceResult, DecisionResponse, NoulResult, ScoreResult
from triage.policy_engine import PolicyDecision


def state_hash(state: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _answer_dict(a: NoulResult | ChoiceResult | ScoreResult) -> dict[str, Any]:
    d = asdict(a)
    if isinstance(a, ChoiceResult):
        d["margin"] = round(a.margin(), 4)
    return d


@dataclass(slots=True)
class DecisionTrace:
    report_id: str
    state_hash: str
    contract_version: str
    contract_hash: str
    model: str
    backend: str
    latency_ms: float
    input_tokens: int | None
    answers: dict[str, Any]
    policy: dict[str, Any]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    executor_receipt: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None

    @classmethod
    def build(cls, report_id: str, state: Mapping[str, Any], questions: Mapping[str, Any],
              resp: DecisionResponse, decision: PolicyDecision) -> "DecisionTrace":
        return cls(
            report_id=report_id, state_hash=state_hash(state),
            contract_version=CONTRACT_VERSION, contract_hash=contract_hash(questions),
            model=resp.model, backend=resp.backend, latency_ms=round(resp.latency_ms, 1),
            input_tokens=resp.input_tokens,
            answers={k: _answer_dict(v) for k, v in resp.answers.items()},
            policy=asdict(decision),
        )

    def append_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(self), default=str) + "\n")
