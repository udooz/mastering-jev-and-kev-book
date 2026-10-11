"""Closed-loop execution: Recommendation -> approval -> authorized, idempotent CMMS action -> verification.

A recommendation is not an authorization. High-consequence actions need a named operator with the
right role; everything is written to the hash-chained audit log; every write is read back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol

from smartplant.audit_log import AuditLog
from smartplant.cmms_mock_service import CmmsUnavailable, InMemoryCmms, MockControlRequests
from smartplant.models import Recommendation

log = logging.getLogger("smartplant.orchestrator")

ROLES_ALLOWED: Mapping[str, frozenset[str]] = {
    "TRANSFER_LOAD": frozenset({"shift_supervisor", "plant_manager"}),
    "REQUEST_SHUTDOWN": frozenset({"shift_supervisor", "plant_manager"}),
}
_RANK = {"ROUTINE": 0, "PRIORITY": 1, "URGENT": 2, "CRITICAL": 3}
Status = Literal["no_action", "executed", "approval_denied", "approval_timeout", "failed_escalated"]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    asset_id: str
    action: str
    urgency: str
    explanation: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    approved: bool | None          # None = no answer before the timeout
    operator_id: str
    role: str
    note: str = ""


class Approver(Protocol):
    def approve(self, req: ApprovalRequest) -> ApprovalDecision: ...


@dataclass(slots=True)
class ScriptedApprover:
    """Tests and replays: a fixed answer, recorded like a real operator's."""
    answer: bool | None = True
    operator_id: str = "op-17"
    role: str = "shift_supervisor"
    seen: list[ApprovalRequest] = field(default_factory=list)

    def approve(self, req: ApprovalRequest) -> ApprovalDecision:
        self.seen.append(req)
        return ApprovalDecision(self.answer, self.operator_id, self.role, "scripted")


class ConsoleApprover:
    """Mini-project 10: prompt the operator in the terminal."""

    def __init__(self, operator_id: str, role: str) -> None:
        self.operator_id, self.role = operator_id, role

    def approve(self, req: ApprovalRequest) -> ApprovalDecision:
        print(f"\n[APPROVAL] {req.action} on {req.asset_id} ({req.urgency})\n  {req.explanation}")
        ans = input("  approve? [y/N/skip] ").strip().lower()
        verdict = True if ans == "y" else None if ans == "skip" else False
        return ApprovalDecision(verdict, self.operator_id, self.role, "console")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: Status
    action_taken: str
    work_order_id: str | None
    control_request_id: str | None
    verified: bool
    detail: str


@dataclass(slots=True)
class ActionOrchestrator:
    cmms: InMemoryCmms
    control: MockControlRequests
    approver: Approver
    audit: AuditLog
    max_write_attempts: int = 2

    def execute(self, rec: Recommendation, state: Mapping[str, Any],
                now: datetime | None = None) -> ExecutionResult:
        now = now or datetime.now(timezone.utc)
        corr = f"{rec.asset_id}:{rec.snapshot_hash}"
        self.audit.append("snapshot", corr, dict(state))
        self.audit.append("decision", corr, rec.to_dict())

        if rec.action == "MONITOR":
            return self._done(corr, ExecutionResult("no_action", "MONITOR", None, None, True, "monitoring only"))

        action, priority, status, approver_id = rec.action, rec.urgency, "executed", None
        if rec.requires_approval:
            d = self.approver.approve(ApprovalRequest(rec.asset_id, rec.action, rec.urgency,
                                                      rec.explanation, rec.reasons))
            authorized = d.approved is True and d.role in ROLES_ALLOWED.get(rec.action, frozenset())
            self.audit.append("approval", corr, {"requested": rec.action, "approved": d.approved,
                                                 "operator": d.operator_id, "role": d.role,
                                                 "authorized": authorized, "note": d.note})
            if not authorized:
                # Safe default: never silently drop the case; inspect instead and say why.
                status = "approval_timeout" if d.approved is None else "approval_denied"
                action = "INSPECT"
                priority = "URGENT" if rec.urgency in ("URGENT", "CRITICAL") else rec.urgency
            else:
                approver_id = d.operator_id

        key = f"{rec.asset_id}:{action}:{now:%Y%m%d%H}"          # one work order per asset/action/hour
        wo_id = self._create_with_retry(key, rec, action, priority, corr)
        if wo_id is None:
            return self._done(corr, ExecutionResult("failed_escalated", action, None, None, False,
                                                    "CMMS unavailable; paging maintenance supervisor"))
        wo_id, verified = self._verify(wo_id, key, rec, action, priority, corr)
        existing = self.cmms.get_work_order(wo_id)
        note = ""
        if verified and existing is not None and _RANK[priority] > _RANK[existing.priority]:
            # Same episode, worse condition: escalate the open work order instead of opening another.
            old = existing.priority
            self.cmms.escalate_priority(wo_id, priority)
            self.audit.append("execution", corr, {"escalated": wo_id, "from": old, "to": priority})
            note = f" (escalated {old}->{priority})"
        cr_id = None
        if verified and approver_id and action in ROLES_ALLOWED:
            cr_id = self.control.submit(action, rec.asset_id, approver_id, wo_id)
        return self._done(corr, ExecutionResult(status, action, wo_id, cr_id, verified,
                                                f"{action} via {wo_id}{note}" + (f" + {cr_id}" if cr_id else "")))

    def record_outcome(self, rec: Recommendation, wo_id: str, finding: str,
                       expert_labels: Mapping[str, Any] | None = None) -> None:
        """Close the loop with what the technician actually found. Expert labels feed fine-tuning (Ch13)."""
        self.cmms.complete(wo_id, finding)
        self.audit.append("outcome", f"{rec.asset_id}:{rec.snapshot_hash}",
                          {"work_order": wo_id, "finding": finding, "expert_labels": dict(expert_labels or {})})

    # ---- internals ----
    def _create_with_retry(self, key: str, rec: Recommendation, action: str, priority: str,
                           corr: str) -> str | None:
        for attempt in range(1, self.max_write_attempts + 1):
            try:
                return self.cmms.create_work_order(key, rec.asset_id, action, priority, rec.explanation,
                                                   {"snapshot_hash": rec.snapshot_hash, "reasons": list(rec.reasons),
                                                    "model": rec.model, "source": rec.source})
            except CmmsUnavailable as e:
                log.warning("CMMS write failed (attempt %d): %s", attempt, e)
                self.audit.append("execution", corr, {"error": str(e), "attempt": attempt})
        return None

    def _verify(self, wo_id: str, key: str, rec: Recommendation, action: str, priority: str,
                corr: str) -> tuple[str, bool]:
        wo = self.cmms.get_work_order(wo_id)
        ok = wo is not None and wo.kind == action and wo.status == "open"
        if not ok:   # acknowledged but not stored: write again under a new key, then re-read
            retry_id = self._create_with_retry(f"{key}:reissue", rec, action, priority, corr)
            if retry_id:
                wo_id = retry_id
                again = self.cmms.get_work_order(retry_id)
                ok = again is not None and again.kind == action
        self.audit.append("verification", corr, {"work_order": wo_id, "verified": ok})
        return wo_id, ok

    def _done(self, corr: str, result: ExecutionResult) -> ExecutionResult:
        self.audit.append("execution", corr, {"status": result.status, "action": result.action_taken,
                                              "work_order": result.work_order_id,
                                              "control_request": result.control_request_id,
                                              "verified": result.verified, "detail": result.detail})
        return result
