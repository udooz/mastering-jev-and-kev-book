"""Mini-project 9: evaluate SmartPlant scenarios with Kev (or the offline stand-in).

DECISION_BACKEND=kev KEV_BASE_URL=http://127.0.0.1:8009 uv run python mini-projects/09-smartplant-decision-service/run_decision_service.py S06
uv run python mini-projects/09-smartplant-decision-service/run_decision_service.py --offline
"""
from __future__ import annotations

import argparse
import json

from triage.jev_client import FakeDecisionClient, client_from_env

from smartplant.context_enricher import summarize
from smartplant.decision_service import SmartPlantDecisionService
from smartplant.scenario_runner import DownClient, build_state, load_scenarios, scripted_model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="scenario ids (default: all)")
    ap.add_argument("--offline", action="store_true", help="use the scripted stand-in instead of a server")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    client = FakeDecisionClient(scripted_model) if args.offline else client_from_env()
    service = SmartPlantDecisionService(client)
    for spec in load_scenarios():
        if args.ids and spec["id"] not in args.ids:
            continue
        state = build_state(spec)
        # S10 simulates the decision service timing out; everything else uses the shared service.
        svc = SmartPlantDecisionService(DownClient()) if spec.get("decision_service_down") else service
        rec = svc.evaluate(state)
        if args.json:
            print(json.dumps({"scenario": spec["id"], **rec.to_dict()}))
            continue
        print(f"\n{spec['id']} {spec['name']}\n  state : {summarize(state)}")
        print(f"  result: {rec.urgency} / {rec.action} / severity {rec.severity} / {rec.route}"
              f"{' / APPROVAL REQUIRED' if rec.requires_approval else ''}  [{rec.source}, {rec.latency_ms} ms]")
        print(f"  why   : {rec.explanation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
