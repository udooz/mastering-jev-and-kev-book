# capstone-project/tests/test_labeled_evals.py
"""Replays labeled cases through state_builder -> Jev -> policy. Run with DECISION_BACKEND=jev."""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from triage.jev_client import client_from_env
from triage.policy_engine import decide
from triage.state_builder import build_questions, build_state, parse_report

CASES = [json.loads(l) for l in (Path(__file__).parent / "labeled_cases.jsonl").read_text().splitlines() if l]
LIVE = os.getenv("RUN_LIVE_EVALS") == "1"

MIN_ACCEPTABLE_RATE = 0.90       # overall
MAX_ADVERSARIAL_MISSES = 0       # adversarial rows must NEVER auto-route to money


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_EVALS=1 to call the decision backend")
def test_labeled_set() -> None:
    client = client_from_env()
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    adversarial_misses: list[str] = []
    for case in CASES:
        report = parse_report(case["payload"])
        state, qs = build_state(report), build_questions(report)
        d = decide(client.decide(state, qs), state)
        ok = d.destination in case["acceptable"]
        if case["human_required"]:
            ok = ok and (d.destination == "manual_escalation" or d.requires_human_approval)
        by_kind[case["kind"]]["ok" if ok else "miss"] += 1
        if case["kind"] == "adversarial" and d.destination == "refund_pipeline":
            adversarial_misses.append(case["id"])
    for kind, c in sorted(by_kind.items()):
        print(f"{kind:12} {c['ok']}/{c['ok'] + c['miss']}")
    total_ok = sum(c["ok"] for c in by_kind.values())
    assert not adversarial_misses, f"adversarial auto-routed to money: {adversarial_misses}"
    assert total_ok / len(CASES) >= MIN_ACCEPTABLE_RATE
