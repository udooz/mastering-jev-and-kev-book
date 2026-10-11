"""Mini-project 12: close the work orders from a controller run with technician findings and expert labels.

The outcomes are appended to the same hash-chained audit log, so `smartplant-export` can pair them with
the snapshot Kev saw and write fine-tuning data (Chapter 13).

  uv run python mini-projects/12-learning-loop/record_outcomes.py audit/smartplant.jsonl            # you are the technician
  uv run python mini-projects/12-learning-loop/record_outcomes.py audit/smartplant.jsonl --demo     # scripted technician
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from smartplant.audit_log import AuditLog, verify_chain
from smartplant.contract import ACTIONS, URGENCIES, build_questions

LABEL_KEYS = ("intervention_urgency", "operational_action", "standard_workflow", "operational_severity")


def open_work_orders(path: Path) -> list[dict[str, Any]]:
    """Latest snapshot + decision for every work order created in the run (one outcome per work order)."""
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_corr: dict[str, dict[str, Any]] = {}
    for r in rows:
        by_corr.setdefault(r["correlation_id"], {})[r["event"]] = r["payload"]
    closed = {r["payload"]["work_order"] for r in rows if r["event"] == "outcome"}
    latest: dict[str, dict[str, Any]] = {}
    for corr, ev in by_corr.items():
        wo = (ev.get("execution") or {}).get("work_order")
        if wo and wo not in closed and "snapshot" in ev and "decision" in ev:
            latest[wo] = {"work_order": wo, "correlation_id": corr, "state": ev["snapshot"], "decision": ev["decision"]}
    return list(latest.values())


def demo_technician(state: dict[str, Any]) -> dict[str, Any]:
    """STAND-IN for a real technician: labels from what the bearing condition turned out to be.

    Never label with the model's own answers; that only teaches Kev to agree with itself.
    """
    zone = state["telemetry"]["vibration_zone"]
    allowed = build_questions(state, speculative=False)["operational_action"]["criteria"]
    if zone.startswith("D"):
        action = "TRANSFER_LOAD" if "TRANSFER_LOAD" in allowed else "REQUEST_SHUTDOWN"
        return {"intervention_urgency": "CRITICAL", "operational_action": action,
                "standard_workflow": False, "operational_severity": 5}
    if zone.startswith("C"):
        return {"intervention_urgency": "URGENT", "operational_action": "SCHEDULE_MAINTENANCE",
                "standard_workflow": True, "operational_severity": 4}
    return {"intervention_urgency": "PRIORITY", "operational_action": "INSPECT",
            "standard_workflow": True, "operational_severity": 2}


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:                       # piped input ran out: treat as "no answer"
        return ""


def ask_technician(state: dict[str, Any]) -> dict[str, Any] | None:
    allowed = list(build_questions(state, speculative=False)["operational_action"]["criteria"])
    print("  (press Enter on any question to leave this work order unlabelled)")
    urgency = _ask(f"  urgency {list(URGENCIES)}: ").upper()
    action = _ask(f"  action {allowed}: ").upper()
    standard = _ask("  standard workflow was enough? [y/n]: ").lower()
    severity = _ask("  severity 1-5: ")
    if not (urgency and action and standard and severity.isdigit()):
        return None
    return {"intervention_urgency": urgency, "operational_action": action,
            "standard_workflow": standard == "y", "operational_severity": int(severity)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record technician outcomes for a controller run")
    ap.add_argument("audit", type=Path, nargs="?", default=Path("audit/smartplant.jsonl"))
    ap.add_argument("--demo", action="store_true", help="use the scripted technician instead of prompting")
    args = ap.parse_args(argv)

    if not args.audit.exists():
        print(f"{args.audit} not found: run the Chapter 11 controller first", file=sys.stderr)
        return 2
    log = AuditLog(args.audit)
    pending = open_work_orders(args.audit)
    if not pending:
        print("no open work orders in this audit log")
        return 0
    for wo in pending:
        t, d = wo["state"]["telemetry"], wo["decision"]
        print(f"\n{wo['work_order']}  {d['asset_id']}: vib {t['vibration_zone']}/{t['vibration_trend']}, "
              f"temp {t['temperature_band']}  | system said {d['urgency']}/{d['action']} sev {d['severity']} [{d['source']}]")
        if args.demo:
            labels, finding = demo_technician(wo["state"]), "demo technician: condition confirmed on site"
        else:
            finding = _ask("  what did you find? ") or "no finding recorded"
            labels = ask_technician(wo["state"])
        bad = labels is not None and (labels["intervention_urgency"] not in URGENCIES
                                      or labels["operational_action"] not in ACTIONS)
        log.append("outcome", wo["correlation_id"], {
            "work_order": wo["work_order"], "finding": finding,
            "expert_labels": {} if labels is None or bad else labels})
        print(f"  -> outcome recorded{' (unlabelled: excluded from training)' if labels is None or bad else ''}: "
              f"{labels if labels and not bad else ''}")
    print(f"\naudit chain intact: {verify_chain(args.audit)}  ({args.audit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
