# mini-projects/01-starter-client/bench.py
from __future__ import annotations

import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass

from triage.jev_client import DecisionClient, DecisionError, Questions, JSONState, client_from_env

JEV_USD_PER_M_INPUT = 0.042


@dataclass(frozen=True, slots=True)
class BenchResult:
    label: str
    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    errors: int
    usd_per_1k: float


def _pct(xs: Sequence[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[k]


def bench(label: str, fn, n: int) -> BenchResult:
    lat: list[float] = []
    tokens: list[int] = []
    errors = 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            used = fn()
        except DecisionError:
            errors += 1
            continue
        lat.append((time.perf_counter() - t0) * 1000)
        tokens.append(used)
    mean_tokens = statistics.fmean(tokens) if tokens else 0.0
    return BenchResult(label, n, _pct(lat, 50), _pct(lat, 95), _pct(lat, 99), errors,
                       mean_tokens * JEV_USD_PER_M_INPUT / 1_000_000 * 1000)


def fan_out(client: DecisionClient, state: JSONState, qs: Questions) -> int:
    r = client.decide(state, qs)
    return r.input_tokens or 0


def sequential(client: DecisionClient, state: JSONState, qs: Questions) -> int:
    total = 0
    for qid, spec in qs.items():
        total += client.decide(state, {qid: spec}).input_tokens or 0
    return total


if __name__ == "__main__":
    from triage.contract import TRIAGE_QUESTIONS  # built in Chapter 2

    client = client_from_env()
    state = {"report": "Two card payments of 49,999 INR I don't recognise, both at 3am. Please block my card."}
    for r in (
        bench("fan-out (1 call, 3 questions)", lambda: fan_out(client, state, TRIAGE_QUESTIONS), 50),
        bench("sequential (3 calls)", lambda: sequential(client, state, TRIAGE_QUESTIONS), 50),
    ):
        print(f"{r.label:32} p50={r.p50_ms:6.0f}ms p95={r.p95_ms:6.0f}ms "
              f"p99={r.p99_ms:6.0f}ms err={r.errors} ${r.usd_per_1k:.5f}/1k")