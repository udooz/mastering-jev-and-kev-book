"""Replay the 10 SmartPlant scenarios through any engine: rules, LLM (adapter), Kev, or an offline stand-in.

Each scenario is rebuilt from scratch (seeded simulator -> edge snapshot -> enrichment), so every
engine sees byte-identical state. Results feed benchmark_metrics and eval_dashboard.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from triage.jev_client import (Answer, ChoiceResult, DecisionClient, DecisionError, DecisionResponse,
                               FakeDecisionClient, NoulResult, Questions, ScoreResult)
from triage.resilience import CircuitBreaker

from smartplant.action_orchestrator import ActionOrchestrator, ScriptedApprover
from smartplant.audit_log import AuditLog, audit_complete
from smartplant.cmms_mock_service import InMemoryCmms, MockControlRequests
from smartplant.context_enricher import AssetRecord, BackupAsset, MockPlantData, ProductionOrder, enrich
from smartplant.decision_service import SmartPlantDecisionService
from smartplant.edge_state_builder import EdgeStateBuilder
from smartplant.fallback_rules import rule_baseline
from smartplant.fault_injector import Fault, FaultMode
from smartplant.models import Recommendation
from smartplant.sensor_simulator import AssetProfile, SensorSimulator


def load_scenarios(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Packaged with smartplant so every mini-project loads it without path tricks."""
    raw = (Path(path).read_text(encoding="utf-8") if path else
           resources.files("smartplant.data").joinpath("test_scenarios.json").read_text(encoding="utf-8"))
    return json.loads(raw)["scenarios"]


def build_state(spec: Mapping[str, Any], asset_id: str = "M-104") -> dict[str, Any]:
    profile = AssetProfile(asset_id=asset_id)
    faults = [Fault(FaultMode(f["mode"]), f["start_step"], f.get("duration_steps"),
                    f.get("magnitude", 1.0), f.get("channel", "bearing_temp_c")) for f in spec["faults"]]
    sim = SensorSimulator(profile, seed=spec["seed"], load=lambda _s: spec["load"], faults=faults)
    builder = EdgeStateBuilder(profile)
    last = None
    for r in sim.stream(spec["steps"]):
        builder.add(r)
        last = r
    assert last is not None
    snap = builder.snapshot(last.ts + timedelta(seconds=5 + spec.get("stale_seconds", 0)))
    c = spec["context"]
    plant = MockPlantData(
        orders={asset_id: ProductionOrder(f"PO-{spec['id']}", c["order"]["priority"],
                                          c["order"]["hours_to_deadline"], c["order"]["required_load_pct"])}
        if c.get("order") else {},
        assets={asset_id: AssetRecord(asset_id, **c["asset"])},
        backups={asset_id: BackupAsset(**c["backup"])},
    )
    return enrich(snap, plant, plant, plant)


# ---------- Engines ----------
class Engine(Protocol):
    name: str

    def recommend(self, state: Mapping[str, Any], spec: Mapping[str, Any]) -> Recommendation: ...


class RuleEngine:
    name = "rules"

    def recommend(self, state: Mapping[str, Any], spec: Mapping[str, Any]) -> Recommendation:
        return rule_baseline(state)


class DownClient:
    """Simulates the decision service timing out (scenario S10)."""
    backend = "down"

    def decide(self, state: Any, questions: Questions) -> DecisionResponse:
        raise DecisionError("timeout after 5.0s", retryable=True)


@dataclass(slots=True)
class ServiceEngine:
    """Any DecisionClient (Kev, Jev, adapter-LLM, fake) behind the same SmartPlant policy."""
    name: str
    client: DecisionClient

    def recommend(self, state: Mapping[str, Any], spec: Mapping[str, Any]) -> Recommendation:
        client: DecisionClient = DownClient() if spec.get("decision_service_down") else self.client
        svc = SmartPlantDecisionService(client, CircuitBreaker(failure_threshold=3, cooldown_s=30))
        return svc.evaluate(state)


def scripted_model(state: Any, questions: Questions) -> Mapping[str, Answer]:
    """OFFLINE STAND-IN for Kev so the harness runs anywhere. Its numbers measure the pipeline, not Kev."""
    t = state["telemetry"]
    sev = {"A_good": 0.2, "B_acceptable": 1.0, "C_alert": 2.0, "D_danger": 3.0, "unknown": 1.5}[t["vibration_zone"]]
    sev = max(sev, {"normal": 0.2, "elevated": 1.5, "high": 3.0, "trip": 4.0, "unknown": 1.5}[t["temperature_band"]])
    if "rising_fast" in (t["vibration_trend"], t["temperature_trend"]):
        sev = min(4.0, sev + 0.6)
    urgency = "ROUTINE" if sev < 0.9 else "PRIORITY" if sev < 2.3 else "URGENT" if sev < 3.6 else "CRITICAL"
    offered = list(questions["operational_action"]["criteria"])
    if sev >= 3.0 and "TRANSFER_LOAD" in offered:
        action = "TRANSFER_LOAD"
    elif sev >= 0.9:
        action = "INSPECT"
    else:
        action = "MONITOR"
    conflict = 0.85 if t["temperature_trend"] in ("rising", "rising_fast") and t["vibration_trend"] == "stable" else 0.1

    def choice(label: str, labels: Sequence[str], conf: float) -> ChoiceResult:
        rest = (1 - 0.6) / max(1, len(labels) - 1)
        return ChoiceResult(label, {l: (0.6 if l == label else rest) for l in labels}, conf)

    return {
        "intervention_urgency": choice(urgency, ["ROUTINE", "PRIORITY", "URGENT", "CRITICAL"], 0.55),
        "operational_action": choice(action, offered, 0.5),
        "standard_workflow": NoulResult(0.3 if conflict > 0.5 else 0.8),
        "operational_severity": ScoreResult(sev, {}, 0.6, 5),
        "signals_conflict": NoulResult(conflict),
    }


# ---------- Runner ----------
@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    scenario_name: str
    engine: str
    recommendation: Recommendation
    wall_ms: float
    audit_complete: bool
    execution_status: str


def run(engines: Sequence[Engine], scenarios: Sequence[Mapping[str, Any]],
        audit_dir: Path | None = None) -> list[ScenarioResult]:
    audit_dir = audit_dir or Path(tempfile.mkdtemp(prefix="smartplant-audit-"))
    out: list[ScenarioResult] = []
    for eng in engines:
        audit = AuditLog(audit_dir / f"{eng.name}.jsonl")
        orch = ActionOrchestrator(InMemoryCmms(), MockControlRequests(), ScriptedApprover(True), audit)
        for spec in scenarios:
            state = build_state(spec)
            t0 = time.perf_counter()
            rec = eng.recommend(state, spec)
            wall = (time.perf_counter() - t0) * 1000
            result = orch.execute(rec, state)
            out.append(ScenarioResult(spec["id"], spec["name"], eng.name, rec, round(wall, 1),
                                      audit_complete(audit, f"{rec.asset_id}:{rec.snapshot_hash}"),
                                      result.status))
    return out


def default_engines(use_kev: bool, use_llm: bool) -> list[Engine]:
    engines: list[Engine] = [RuleEngine()]
    if use_llm:
        from triage.jev_client import AdapterDecisionClient
        engines.append(ServiceEngine("llm-gpt-4o-mini", AdapterDecisionClient("openai", "gpt-4o-mini")))
    if use_kev:
        from triage.jev_client import KevDecisionClient
        engines.append(ServiceEngine("kev", KevDecisionClient()))
    if not use_kev:
        engines.append(ServiceEngine("kev-offline-standin", FakeDecisionClient(scripted_model)))
    return engines


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay SmartPlant scenarios across engines")
    ap.add_argument("--kev", action="store_true", help="use a running Kev server (KEV_BASE_URL)")
    ap.add_argument("--llm", action="store_true", help="add the LLM baseline via system-one-adapter")
    ap.add_argument("--scenarios", default=None, help="path to a scenarios JSON (default: packaged)")
    ap.add_argument("--out", default="eval_results.json")
    args = ap.parse_args(argv)
    results = run(default_engines(args.kev, args.llm), load_scenarios(args.scenarios))
    rows = [{"scenario": r.scenario_id, "name": r.scenario_name, "engine": r.engine,
             "wall_ms": r.wall_ms, "audit_complete": r.audit_complete, "execution": r.execution_status,
             **r.recommendation.to_dict()} for r in results]
    Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    for r in results:
        rec = r.recommendation
        print(f"{r.scenario_id} {r.engine:22} {rec.urgency:9} {rec.action:21} sev={rec.severity} "
              f"{rec.route:18} src={rec.source:12} {r.wall_ms:7.1f}ms")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
