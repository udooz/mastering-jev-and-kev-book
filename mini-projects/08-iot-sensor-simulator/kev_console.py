"""Replay edge snapshots through Kev and print what it decides, one line per snapshot.

  uv run python mini-projects/08-iot-sensor-simulator/kev_console.py snapshots.jsonl --raw       # Kev's raw answers
  uv run python mini-projects/08-iot-sensor-simulator/kev_console.py snapshots.jsonl             # full SmartPlant decision
  uv run python mini-projects/08-iot-sensor-simulator/kev_console.py snapshots.jsonl --offline   # no Kev server needed
  uv run python mini-projects/08-iot-sensor-simulator/kev_console.py snapshots.jsonl --show-state 5   # what Kev sees
  uv run python mini-projects/08-iot-sensor-simulator/kev_console.py snapshots.jsonl --backup --order HIGH
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from triage.jev_client import DecisionClient, DecisionError, FakeDecisionClient, KevDecisionClient

from smartplant.context_enricher import AssetRecord, BackupAsset, MockPlantData, ProductionOrder, enrich
from smartplant.contract import build_questions
from smartplant.decision_service import SmartPlantDecisionService
from smartplant.edge_state_builder import EdgeSnapshot
from smartplant.scenario_runner import scripted_model

# Three plain questions straight from the edge snapshot (Chapter 9 level: no plant context yet).
RAW_QUESTIONS: dict[str, dict[str, Any]] = {
    "needs_inspection": {
        "type": "noul",
        "instructions": "Should an operator physically inspect `asset_id` this shift, given `bands` and `features`?",
    },
    "condition": {
        "type": "choice",
        "instructions": "Which best describes the condition of `asset_id` from `bands` and `features`?",
        "criteria": {
            "healthy": "Vibration and temperature in normal bands with stable trends",
            "early_wear": "One signal outside its normal band or trending up; no limit is near",
            "degrading": "Signals in alert bands or rising fast toward a limit",
            "severe": "Danger-zone vibration or high temperature; damage likely soon",
        },
    },
    "severity": {
        "type": "score",
        "instructions": "How severe is the condition of `asset_id`?",
        "criteria": ["No abnormal condition", "Minor deviation", "Degraded, will worsen",
                     "Serious, risks damage", "Protective limit reached or imminent"],
    },
}


def load_snapshots(path: Path) -> list[EdgeSnapshot]:
    snaps = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            snaps.append(EdgeSnapshot(**{**d, "quality_issues": tuple(d["quality_issues"])}))
    return snaps


def demo_plant(asset_id: str, order: str = "CRITICAL", backup: bool = False) -> MockPlantData:
    """Pretend MES/CMMS/redundancy data. Default: a critical order is running and there is no backup motor."""
    orders = {} if order == "NONE" else {
        asset_id: ProductionOrder("PO-7781", order, hours_to_deadline=6.0, required_load_pct=0.8)}  # type: ignore[arg-type]
    return MockPlantData(
        orders=orders,
        assets={asset_id: AssetRecord(asset_id, "A", days_since_bearing_replacement=410,
                                      open_work_orders=0, spare_bearing_in_stock=True)},
        backups={asset_id: BackupAsset("M-105", True, True, 0.95) if backup else BackupAsset(None, False, False, 0.0)},
    )


def raw_line(client: DecisionClient, s: EdgeSnapshot) -> str:
    r = client.decide(s.to_dict(), RAW_QUESTIONS)
    c, sev = r.choice("condition"), r.score("severity")
    return (f"inspect? p={r.noul('needs_inspection').p_yes:4.2f}  condition={c.choice:10} "
            f"(conf {c.confidence:4.2f})  severity={sev.score:3.1f}/4  {r.latency_ms:5.0f} ms")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print Kev's decision for every snapshot")
    ap.add_argument("snapshots", type=Path, nargs="?", default=Path("snapshots.jsonl"))
    ap.add_argument("--raw", action="store_true", help="ask three plain questions instead of the full contract")
    ap.add_argument("--offline", action="store_true", help="scripted stand-in instead of a Kev server")
    ap.add_argument("--show-state", type=int, metavar="N", default=None,
                    help="print the enriched state and questions for snapshot N (1-based) and exit")
    ap.add_argument("--backup", action="store_true", help="a backup motor (M-105) can take the load")
    ap.add_argument("--order", default="CRITICAL", choices=["NONE", "LOW", "NORMAL", "HIGH", "CRITICAL"],
                    help="priority of the running production order")
    args = ap.parse_args(argv)

    snaps = load_snapshots(args.snapshots)
    if not snaps:
        print(f"{args.snapshots} has no snapshots", file=sys.stderr)
        return 2
    plant = demo_plant(snaps[0].asset_id, args.order, args.backup)
    if args.show_state is not None:
        state = enrich(snaps[args.show_state - 1], plant, plant, plant)
        print(json.dumps({"state": state, "questions": build_questions(state)}, indent=2))
        return 0
    if args.offline and args.raw:
        print("--raw needs a real Kev server (the stand-in only answers the SmartPlant contract)", file=sys.stderr)
        return 2
    client: DecisionClient = FakeDecisionClient(scripted_model) if args.offline else KevDecisionClient()
    service = SmartPlantDecisionService(client)

    for s in snaps:
        minute = s.window_end[11:16]
        head = f"{minute} vib={s.latest['vibration_rms_mm_s']!s:>5} {s.bands['vibration_zone']:12} {s.bands['vibration_trend']:11}"
        try:
            if args.raw:
                print(f"{head} | {raw_line(client, s)}")
                continue
            rec = service.evaluate(enrich(s, plant, plant, plant))
        except DecisionError as e:
            print(f"{head} | decision failed: {e}  (is Kev running at KEV_BASE_URL?)")
            continue
        flag = "  APPROVAL REQUIRED" if rec.requires_approval else ""
        print(f"{head} | {rec.urgency:8} {rec.action:20} sev {rec.severity}  [{rec.source}]{flag}")
    if not args.raw:
        print(f"\nlast explanation: {rec.explanation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
