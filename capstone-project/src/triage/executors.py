# capstone-project/src/triage/executors.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from triage.policy_engine import PolicyDecision


@dataclass(frozen=True, slots=True)
class Receipt:
    owner: str
    artifact_id: str
    acceptance: str
    evidence_uri: str
    escalation: str
    attempted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---- Ports to the outside world (real clients in prod, in-memory fakes in tests) ----
class HelpdeskAPI(Protocol):
    def upsert_ticket(self, key: str, queue: str, priority: str, body: Mapping[str, Any]) -> str: ...
    def get_ticket(self, ticket_id: str) -> Mapping[str, Any] | None: ...


class SecOpsAPI(Protocol):
    def open_case(self, key: str, priority: str, evidence: Mapping[str, Any]) -> str: ...
    def get_case(self, case_id: str) -> Mapping[str, Any] | None: ...


class LedgerAPI(Protocol):
    def create_refund_request(self, key: str, needs_approval: bool, evidence: Mapping[str, Any]) -> str: ...
    def get_refund_request(self, refund_id: str) -> Mapping[str, Any] | None: ...


class Executor(Protocol):
    target: str

    def execute(self, report_id: str, state: Mapping[str, Any], d: PolicyDecision) -> Receipt: ...


@dataclass(slots=True)
class Tier1Executor:
    helpdesk: HelpdeskAPI
    target: str = "tier1_support"

    def execute(self, report_id: str, state: Mapping[str, Any], d: PolicyDecision) -> Receipt:
        tid = self.helpdesk.upsert_ticket(f"triage:{report_id}", "tier1", d.priority, state)
        return Receipt("tier1_support", tid, "ticket exists in queue tier1 with matching priority",
                       f"helpdesk://tickets/{tid}", "unassigned after 4h -> manual_escalation")


@dataclass(slots=True)
class SecOpsExecutor:
    secops: SecOpsAPI
    target: str = "secops_agent"

    def execute(self, report_id: str, state: Mapping[str, Any], d: PolicyDecision) -> Receipt:
        cid = self.secops.open_case(f"triage:{report_id}", d.priority,
                                    {"signals": state.get("computed_signals"), "reasons": d.reasons})
        return Receipt("secops_agent", cid, "case status in {open, investigating} and priority set",
                       f"secops://cases/{cid}", "P1 not acknowledged in 15 min -> on-call page")


@dataclass(slots=True)
class RefundExecutor:
    ledger: LedgerAPI
    target: str = "refund_pipeline"

    def execute(self, report_id: str, state: Mapping[str, Any], d: PolicyDecision) -> Receipt:
        # The executor NEVER computes an amount from model output; the ledger derives it from txn ids.
        rid = self.ledger.create_refund_request(f"triage:{report_id}", d.requires_human_approval,
                                                {"merchants": state.get("disputed_merchants")})
        accept = "status == pending_approval" if d.requires_human_approval else "status in {queued, processed}"
        return Receipt("refund_pipeline", rid, accept, f"ledger://refunds/{rid}",
                       "ledger rejects or 2 identical failures -> manual_escalation")


@dataclass(slots=True)
class ManualEscalationExecutor:
    helpdesk: HelpdeskAPI
    target: str = "manual_escalation"

    def execute(self, report_id: str, state: Mapping[str, Any], d: PolicyDecision) -> Receipt:
        tid = self.helpdesk.upsert_ticket(f"triage:{report_id}", "escalations", d.priority,
                                          {"state": state, "reasons": d.reasons})
        return Receipt("manual_escalation", tid, "ticket exists in queue escalations",
                       f"helpdesk://tickets/{tid}", "n/a (already human)")
