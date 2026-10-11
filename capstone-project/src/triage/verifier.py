# capstone-project/src/triage/verifier.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from triage.executors import HelpdeskAPI, LedgerAPI, Receipt, SecOpsAPI
from triage.policy_engine import PolicyDecision

Outcome = Literal["verified", "not_found", "mismatch"]


@dataclass(frozen=True, slots=True)
class Verification:
    outcome: Outcome
    detail: str

    @property
    def ok(self) -> bool:
        return self.outcome == "verified"


@dataclass(slots=True)
class FreshStateVerifier:
    helpdesk: HelpdeskAPI
    secops: SecOpsAPI
    ledger: LedgerAPI

    def verify(self, receipt: Receipt, d: PolicyDecision) -> Verification:
        if receipt.owner in ("tier1_support", "manual_escalation"):
            t = self.helpdesk.get_ticket(receipt.artifact_id)
            if t is None:
                return Verification("not_found", "ticket missing")
            want_q = "tier1" if receipt.owner == "tier1_support" else "escalations"
            if t.get("queue") != want_q or t.get("priority") != d.priority:
                return Verification("mismatch", f"queue={t.get('queue')} priority={t.get('priority')}")
            return Verification("verified", f"ticket {receipt.artifact_id} in {want_q}")
        if receipt.owner == "secops_agent":
            c = self.secops.get_case(receipt.artifact_id)
            if c is None:
                return Verification("not_found", "case missing")
            if c.get("status") not in {"open", "investigating"}:
                return Verification("mismatch", f"status={c.get('status')}")
            return Verification("verified", f"case {receipt.artifact_id} {c.get('status')}")
        if receipt.owner == "refund_pipeline":
            r = self.ledger.get_refund_request(receipt.artifact_id)
            if r is None:
                return Verification("not_found", "refund request missing")
            allowed = {"pending_approval"} if d.requires_human_approval else {"queued", "processed"}
            if r.get("status") not in allowed:
                return Verification("mismatch", f"status={r.get('status')} expected {sorted(allowed)}")
            return Verification("verified", f"refund {receipt.artifact_id} {r.get('status')}")
        return Verification("mismatch", f"unknown owner {receipt.owner}")
