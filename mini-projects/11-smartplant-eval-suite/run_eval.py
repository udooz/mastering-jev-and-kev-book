"""Mini-project 11: replay the 10 scenarios across engines and build the dashboard.

uv run python mini-projects/11-smartplant-eval-suite/run_eval.py                  # rules + offline stand-in
uv run python mini-projects/11-smartplant-eval-suite/run_eval.py --kev --llm      # real Kev + LLM baseline
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from smartplant.benchmark_metrics import compute, kev_cost_per_state
from smartplant.eval_dashboard import write
from smartplant.scenario_runner import default_engines, load_scenarios, run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kev", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--gpu-usd-per-hour", type=float, default=2.50, help="Kev box cost (amortised)")
    ap.add_argument("--kev-rps", type=float, default=20.0, help="measured Kev throughput, decisions/s")
    ap.add_argument("--llm-usd-per-state", type=float, default=0.00025, help="measured from your token usage")
    ap.add_argument("--out", type=Path, default=Path("smartplant_eval.html"))
    args = ap.parse_args()

    scenarios = load_scenarios()
    results = run(default_engines(args.kev, args.llm), scenarios)
    kev_cost = kev_cost_per_state(args.gpu_usd_per_hour, args.kev_rps)
    costs = {"kev": kev_cost, "kev-offline-standin": 0.0, "llm-gpt-4o-mini": args.llm_usd_per_state}
    metrics = compute(results, scenarios, costs)
    for m in metrics:
        print(json.dumps(m.to_dict()))
    print(f"dashboard: {write(args.out, metrics, results, scenarios).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
