from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from smartplant.action_orchestrator import ActionOrchestrator, ScriptedApprover
from smartplant.audit_log import AuditLog, audit_complete, verify_chain
from smartplant.cmms_mock_service import InMemoryCmms, MockControlRequests
from smartplant.fallback_rules import rule_baseline
from smartplant.models import Recommendation

NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def orch(tmp_path: Path, answer=True, role="shift_supervisor", cmms: InMemoryCmms | None = None):
    approver = ScriptedApprover(answer, role=role)
    return ActionOrchestrator(cmms or InMemoryCmms(), MockControlRequests(), approver,
                              AuditLog(tmp_path / "audit.jsonl")), approver


def as_action(rec: Recommendation, action: str) -> Recommendation:
    return Recommendation(**{**rec.to_dict(), "reasons": rec.reasons, "action": action,
                             "requires_approval": action in ("TRANSFER_LOAD", "REQUEST_SHUTDOWN")})


def test_monitor_creates_nothing(tmp_path, state_for) -> None:
    o, _ = orch(tmp_path)
    st = state_for("S01")
    res = o.execute(rule_baseline(st), st, NOW)
    assert res.status == "no_action" and not o.cmms.work_orders


def test_inspect_creates_verified_work_order_idempotently(tmp_path, state_for) -> None:
    o, _ = orch(tmp_path)
    st = state_for("S02")
    rec = rule_baseline(st)
    a, b = o.execute(rec, st, NOW), o.execute(rec, st, NOW)
    assert a.verified and a.work_order_id == b.work_order_id and len(o.cmms.work_orders) == 1


def test_transfer_needs_authorized_approval(tmp_path, state_for) -> None:
    st = state_for("S06")
    rec = as_action(rule_baseline(st), "TRANSFER_LOAD")
    o, approver = orch(tmp_path)
    ok = o.execute(rec, st, NOW)
    assert ok.status == "executed" and ok.control_request_id and approver.seen
    o2, _ = orch(tmp_path / "b", role="maintenance_tech")          # wrong role
    denied = o2.execute(rec, st, NOW)
    assert denied.status == "approval_denied" and denied.action_taken == "INSPECT"
    o3, _ = orch(tmp_path / "c", answer=None)                       # nobody answered
    assert o3.execute(rec, st, NOW).status == "approval_timeout"


def test_cmms_outage_and_lost_write(tmp_path, state_for) -> None:
    st = state_for("S02")
    rec = rule_baseline(st)
    o, _ = orch(tmp_path, cmms=InMemoryCmms(fail_next=5))
    assert o.execute(rec, st, NOW).status == "failed_escalated"
    o2, _ = orch(tmp_path / "lost", cmms=InMemoryCmms(drop_next_write=True))
    res = o2.execute(rec, st, NOW)
    assert res.verified and res.work_order_id in o2.cmms.work_orders


def test_audit_chain_is_complete_and_tamper_evident(tmp_path, state_for) -> None:
    o, _ = orch(tmp_path)
    st = state_for("S02")
    rec = rule_baseline(st)
    res = o.execute(rec, st, NOW)
    o.record_outcome(rec, res.work_order_id, "outer race spalling confirmed; bearing replaced")
    path = tmp_path / "audit.jsonl"
    assert verify_chain(path) and audit_complete(o.audit, f"{rec.asset_id}:{rec.snapshot_hash}")
    lines = path.read_text().splitlines()
    rec1 = json.loads(lines[1])
    rec1["payload"]["urgency"] = "ROUTINE"                           # someone "fixes" history
    lines[1] = json.dumps(rec1)
    path.write_text("\n".join(lines) + "\n")
    assert not verify_chain(path)


def test_outcomes_with_expert_labels_become_kev_training_data(tmp_path, state_for) -> None:
    from smartplant.training_export import examples_from_audit, write_split
    o, _ = orch(tmp_path)
    st = state_for("S02")
    rec = rule_baseline(st)
    res = o.execute(rec, st, NOW)
    o.record_outcome(rec, res.work_order_id, "outer race spalling",
                     {"intervention_urgency": "URGENT", "operational_action": "SCHEDULE_MAINTENANCE",
                      "standard_workflow": True, "operational_severity": 4})
    ex = examples_from_audit((tmp_path / "audit.jsonl").read_text().splitlines())
    assert len(ex) == 1
    q = ex[0]["questions"]
    assert q["operational_severity"]["label"] == 3 and q["standard_workflow"]["label"] is True
    train, held = write_split(ex, tmp_path / "ft")
    assert train.read_text().count("\n") == 1


def test_worse_condition_escalates_open_work_order(tmp_path, state_for) -> None:
    o, _ = orch(tmp_path)
    st = state_for("S02")
    first = rule_baseline(st)                                         # PRIORITY / INSPECT
    worse = Recommendation(**{**first.to_dict(), "reasons": first.reasons, "urgency": "URGENT"})
    a = o.execute(first, st, NOW)
    b = o.execute(worse, st, NOW)
    assert a.work_order_id == b.work_order_id and "escalated PRIORITY->URGENT" in b.detail
    assert o.cmms.work_orders[a.work_order_id].priority == "URGENT"
