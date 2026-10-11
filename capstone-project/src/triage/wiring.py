# capstone-project/src/triage/wiring.py
from __future__ import annotations

import os
from pathlib import Path

from triage.cascade import CascadeDecider
from triage.engine import TriageEngine
from triage.executors import ManualEscalationExecutor, RefundExecutor, SecOpsExecutor, Tier1Executor
from triage.jev_client import AdapterDecisionClient, DecisionClient, JevDecisionClient
from triage.resilience import CircuitBreaker, ResilientDecider
from triage.verifier import FreshStateVerifier


def build_engine(helpdesk, secops, ledger, primary: DecisionClient | None = None) -> TriageEngine:
    primary = primary or JevDecisionClient(model=os.getenv("JEV_MODEL", "jev-1.13.0"))
    client: DecisionClient = primary
    if os.getenv("CASCADE_ENABLED") == "1":
        client = CascadeDecider(fast=primary,
                                strong=AdapterDecisionClient("openai", os.getenv("CASCADE_MODEL", "gpt-4o")))
    decider = ResilientDecider(client, CircuitBreaker(failure_threshold=5, cooldown_s=30))
    return TriageEngine(
        decider=decider,
        executors={
            "tier1_support": Tier1Executor(helpdesk),
            "secops_agent": SecOpsExecutor(secops),
            "refund_pipeline": RefundExecutor(ledger),
            "manual_escalation": ManualEscalationExecutor(helpdesk),
        },
        verifier=FreshStateVerifier(helpdesk, secops, ledger),
        trace_path=Path(os.getenv("TRACE_PATH", "traces/decisions.jsonl")),
    )
