"""Mini-project 10: closed loop with operator approval and a Kev outage mid-run.

uv run python mini-projects/10-closed-loop-controller/run_controller.py --offline --auto-approve
DECISION_BACKEND=kev uv run python mini-projects/10-closed-loop-controller/run_controller.py --kev-down-at 120
uv run python mini-projects/10-closed-loop-controller/run_controller.py --offline --steps 61 --every 6 --fault-start 6 --magnitude 2
"""
from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

from triage.jev_client import DecisionClient, FakeDecisionClient, client_from_env
from triage.resilience import CircuitBreaker

from smartplant.action_orchestrator import ActionOrchestrator, ConsoleApprover, ScriptedApprover
from smartplant.audit_log import AuditLog, verify_chain
from smartplant.cmms_mock_service import InMemoryCmms, MockControlRequests
from smartplant.context_enricher import AssetRecord, BackupAsset, MockPlantData, ProductionOrder, enrich, summarize
from smartplant.decision_service import SmartPlantDecisionService
from smartplant.edge_state_builder import EdgeStateBuilder
from smartplant.fault_injector import Fault, FaultMode
from smartplant.scenario_runner import DownClient, scripted_model
from smartplant.sensor_simulator import AssetProfile, SensorSimulator


class SwitchableClient:
    """Lets the demo pull the plug on the decision service at a chosen step."""

    def __init__(self, inner: DecisionClient) -> None:
        self.inner, self.down, self.backend = inner, False, inner.backend

    def decide(self, state, questions):
        return (DownClient() if self.down else self.inner).decide(state, questions)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=360)
    ap.add_argument("--every", type=int, default=30, help="decide every N steps (or on band change)")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--kev-down-at", type=int, default=None)
    ap.add_argument("--fault-start", type=int, default=40, help="step where bearing wear begins")
    ap.add_argument("--magnitude", type=float, default=0.8, help="wear rate (2 reaches zone D in ~9 minutes)")
    ap.add_argument("--audit", type=Path, default=Path("audit/smartplant.jsonl"))
    args = ap.parse_args()

    profile = AssetProfile("M-104")
    sim = SensorSimulator(profile, load=lambda _s: 0.9,
                          faults=[Fault(FaultMode.GRADUAL_DEGRADATION, args.fault_start, magnitude=args.magnitude)])
    builder = EdgeStateBuilder(profile)
    plant = MockPlantData(
        orders={"M-104": ProductionOrder("PO-7781", "HIGH", 30, 0.85)},
        assets={"M-104": AssetRecord("M-104", "A", 410, 0, True)},
        backups={"M-104": BackupAsset("M-105", True, True, 0.95)},
    )
    client = SwitchableClient(FakeDecisionClient(scripted_model) if args.offline else client_from_env())
    service = SmartPlantDecisionService(client, CircuitBreaker(failure_threshold=2, cooldown_s=60))
    approver = ScriptedApprover(True) if args.auto_approve else ConsoleApprover("op-17", "shift_supervisor")
    orch = ActionOrchestrator(InMemoryCmms(), MockControlRequests(), approver, AuditLog(args.audit))

    last_key: tuple[object, ...] | None = None
    pending: tuple[object, ...] | None = None
    pending_count = 0
    for r in sim.stream(args.steps):
        builder.add(r)
        if args.kev_down_at is not None and r.seq == args.kev_down_at:
            client.down = True
            print(f"--- t={r.seq}: decision service unreachable ---")
        if r.seq < builder.limits.min_samples:          # warm-up: not enough evidence yet
            continue
        snap = builder.snapshot(r.ts + timedelta(seconds=1))
        # Decide on a meaningful change (zones/bands/quality/trip) or on a heartbeat,
        # not on every 10 s sample: trends jitter, zones don't.
        key = (snap.bands["vibration_zone"], snap.bands["temperature_band"], snap.data_quality,
               snap.protective_limit_reached)
        if key != last_key and not snap.protective_limit_reached:
            # Debounce: a new zone must hold for 3 samples (30 s) before we act on it.
            pending_count = pending_count + 1 if key == pending else 1
            pending = key
            if pending_count < 3:
                continue
        elif r.seq % args.every != 0 and key == last_key:
            continue
        last_key = key
        state = enrich(snap, plant, plant, plant)
        rec = service.evaluate(state)
        res = orch.execute(rec, state, now=r.ts)
        print(f"t={r.seq:4d} {summarize(state)}\n       -> {rec.urgency}/{rec.action} [{rec.source}] "
              f"=> {res.status} {res.detail}")
        if snap.protective_limit_reached:
            print("PLC protective trip latched: motor stopped by the PLC; loop ends until operator reset.")
            break
    print(f"audit chain intact: {verify_chain(args.audit)}  ({args.audit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
