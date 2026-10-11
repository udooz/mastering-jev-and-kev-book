from __future__ import annotations

from smartplant.benchmark_metrics import compute, kev_cost_per_state
from smartplant.eval_dashboard import render
from smartplant.scenario_runner import RuleEngine, ServiceEngine, load_scenarios, run, scripted_model
from triage.jev_client import FakeDecisionClient


def test_ten_scenarios_replay_and_score(tmp_path) -> None:
    scenarios = load_scenarios()
    assert len(scenarios) == 10
    results = run([RuleEngine(), ServiceEngine("standin", FakeDecisionClient(scripted_model))], scenarios, tmp_path)
    metrics = {m.engine: m for m in compute(results, scenarios, {"standin": kev_cost_per_state(2.5, 100)})}
    for m in metrics.values():
        assert m.response_validity == 1.0 and m.audit_completeness == 1.0
        assert m.critical_miss_rate == 0.0 and m.unnecessary_intervention_rate == 0.0
    assert metrics["standin"].fallback_success == 1.0
    html = render(list(metrics.values()), results, scenarios)
    assert "S10" in html and "Critical miss" in html


def test_kev_cost_model() -> None:
    assert round(kev_cost_per_state(3.6, 100), 8) == 0.00001
