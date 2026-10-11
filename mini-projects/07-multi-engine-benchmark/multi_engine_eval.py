# mini-projects/07-multi-engine-benchmark/multi_engine_eval.py
"""Run the SAME triage contract on the SAME labeled cases across several engines.

The labeled cases are the Chapter 5 regression set: capstone-project/tests/labeled_cases.jsonl
(one JSON object per line: payload, expected, acceptable, kind, human_required). Used by default.

  uv run python mini-projects/07-multi-engine-benchmark/multi_engine_eval.py --engines offline
  uv run python mini-projects/07-multi-engine-benchmark/multi_engine_eval.py --engines kev,offline
  uv run python mini-projects/07-multi-engine-benchmark/multi_engine_eval.py --engines jev,openai-decisions,kev
  uv run python mini-projects/07-multi-engine-benchmark/multi_engine_eval.py --cases my_labels.jsonl --engines jev,adapter

Writes benchmark_results.json next to this script (input for benchmark_comparison_plot.py).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from triage.jev_client import Answer, ChoiceResult, DecisionClient, DecisionError, FakeDecisionClient, NoulResult, ScoreResult
from triage.state_builder import build_questions, build_state, parse_report

LABELS = ("tier1_support", "secops_agent", "refund_pipeline", "manual_escalation")
HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE.parents[1] / "capstone-project" / "tests" / "labeled_cases.jsonl"
DEFAULT_OUT = HERE / "benchmark_results.json"


@dataclass(frozen=True, slots=True)
class EngineReport:
    engine: str
    n: int
    accuracy: float
    schema_error_rate: float
    p50_ms: float
    p95_ms: float
    usd_per_1k: float
    ece: float
    brier: float


def ece(conf: Sequence[float], correct: Sequence[bool], bins: int = 10) -> float:
    total, err = len(conf), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(conf) if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if idx:
            acc = sum(correct[i] for i in idx) / len(idx)
            avg = sum(conf[i] for i in idx) / len(idx)
            err += len(idx) / total * abs(acc - avg)
    return err


def brier(probs: Sequence[Mapping[str, float]], gold: Sequence[str]) -> float:
    return statistics.fmean(sum((p.get(l, 0.0) - (l == g)) ** 2 for l in LABELS) for p, g in zip(probs, gold, strict=True))


def run_engine(name: str, client: DecisionClient, cases: Sequence[Mapping[str, Any]],
               usd_per_m_input: float, fallback_tokens: int = 450) -> EngineReport:
    lat, conf, correct, probs, gold = [], [], [], [], []
    schema_errors, tokens = 0, []
    for c in cases:
        report = parse_report(c["payload"])
        state, qs = build_state(report), build_questions(report, speculative=False)
        try:
            r = client.decide(state, qs)
            dt = r.choice("dispatch_target")
        except (DecisionError, KeyError, TypeError):
            schema_errors += 1
            continue
        lat.append(r.latency_ms)
        tokens.append(r.input_tokens or fallback_tokens)
        conf.append(dt.confidence)
        correct.append(dt.choice in c["acceptable"])
        probs.append(dict(dt.probabilities))
        gold.append(c["expected"])
    ok = len(lat)
    q = statistics.quantiles(lat, n=20) if ok >= 2 else [float("nan")] * 19
    return EngineReport(
        engine=name, n=len(cases),
        accuracy=sum(correct) / ok if ok else 0.0,
        schema_error_rate=schema_errors / len(cases),
        p50_ms=statistics.median(lat) if lat else float("nan"), p95_ms=q[18],
        usd_per_1k=statistics.fmean(tokens) * usd_per_m_input / 1_000_000 * 1000 if tokens else 0.0,
        ece=ece(conf, correct) if ok else float("nan"),
        brier=brier(probs, gold) if ok else float("nan"),
    )


def offline_model(state: Any, questions: Mapping[str, Any]) -> Mapping[str, Answer]:
    """OFFLINE STAND-IN: keyword routing so the harness runs without keys. Measures the harness, not a model."""
    body = state["report"]["body"].lower()
    if re.search(r"system:|ignore (previous|all) instructions|i am the admin", body):
        target, conf = "manual_escalation", 0.6
    elif re.search(r"never (asked|made)|didn'?t make|not me|unauthori[sz]ed|otp", body):
        target, conf = "secops_agent", 0.85
    elif re.search(r"charged twice|duplicate|refund|charge .* wrong|looks wrong", body):
        target, conf = "refund_pipeline", 0.7
    elif re.search(r"passed away|lawyer|regulator|ombudsman", body):
        target, conf = "manual_escalation", 0.8
    else:
        target, conf = "tier1_support", 0.75
    offered = list(questions["dispatch_target"]["criteria"])
    if target not in offered:                          # e.g. refund_pipeline pruned: nothing posted
        target, conf = "manual_escalation", 0.5
    rest = (1 - conf) / max(1, len(offered) - 1)
    probs = {l: (conf if l == target else rest) for l in offered}
    k = len(offered)
    return {
        "is_fraud_suspected": NoulResult(0.9 if target == "secops_agent" else 0.05),
        "urgency_rating": ScoreResult(3.5 if target == "secops_agent" else 1.5, {}, 0.6, 5),
        "dispatch_target": ChoiceResult(target, probs, (conf - 1 / k) / (1 - 1 / k)),
    }


ENGINES = ("offline", "jev", "kev", "openai-decisions", "adapter")


def build_engines(names: Sequence[str]) -> list[tuple[str, DecisionClient, float]]:
    """name -> (label, client, USD per million input tokens). Clients are created only when selected.

    Prices are list prices at the time of writing (Oct 2026): re-check them before you publish a number.
    """
    from triage.jev_client import AdapterDecisionClient, JevDecisionClient, KevDecisionClient
    engines: list[tuple[str, DecisionClient, float]] = []
    for name in names:
        if name == "offline":
            engines.append(("offline-standin", FakeDecisionClient(offline_model), 0.0))
        elif name == "jev":
            engines.append(("jev-1.13.0", JevDecisionClient(model="jev-1.13.0"), 0.042))
        elif name == "kev":
            engines.append(("kev (local)", KevDecisionClient(), 0.0))            # you pay for the box, not tokens
        elif name == "openai-decisions":
            from openai_decisions_client import OpenAIDecisionsClient           # uv sync --group ch7; OPENAI_API_KEY
            engines.append(("gpt-6-luna (decisions)", OpenAIDecisionsClient(), 0.10))  # output tokens are free
        elif name == "adapter":
            engines.append(("gpt-4o-mini (adapter)", AdapterDecisionClient("openai", "gpt-4o-mini"), 0.15))
        else:
            raise SystemExit(f"unknown engine {name!r}; choose from {', '.join(ENGINES)}")
    return engines


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark decision engines on one labeled set")
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES,
                    help=f"labeled JSONL (default: {DEFAULT_CASES.relative_to(HERE.parents[1])})")
    ap.add_argument("--engines", default="offline",
                    help=f"comma list from: {', '.join(ENGINES)} (default: offline)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    cases = [json.loads(l) for l in args.cases.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not cases:
        raise SystemExit(f"{args.cases} has no cases")
    try:
        engines = build_engines([e.strip() for e in args.engines.split(",") if e.strip()])
    except (ImportError, RuntimeError) as e:                 # missing SDK group or credentials
        raise SystemExit(f"cannot create engine: {e}") from e
    reports = [run_engine(n, c, cases, price) for n, c, price in engines]
    args.out.write_text(json.dumps([asdict(r) for r in reports], indent=2), encoding="utf-8")

    print(f"{len(cases)} labeled cases from {args.cases.name}\n")
    print(f"{'engine':24} {'acc':>6} {'schema':>7} {'p50':>7} {'p95':>7} {'$/1k':>9} {'ECE':>6} {'Brier':>6}")
    for r in reports:
        print(f"{r.engine:24} {r.accuracy:6.1%} {r.schema_error_rate:7.1%} {r.p50_ms:6.0f}ms {r.p95_ms:6.0f}ms "
              f"{r.usd_per_1k:9.5f} {r.ece:6.3f} {r.brier:6.3f}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
