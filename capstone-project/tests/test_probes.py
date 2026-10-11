"""Probes run against a fake backend that mimics the behaviours reported for Jev."""
from __future__ import annotations

import math
from typing import Any

from triage.jev_client import ChoiceResult, FakeDecisionClient, NoulResult
from triage.probes import choice_confidence, run_all

BASE = {"technical": 2.0, "billing": 1.2, "sales": 0.1, "other": 0.0, "facilities": -0.5}


def mimic(state: Any, questions: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, q in questions.items():
        if q["type"] == "noul":
            out[qid] = NoulResult(0.2)
            continue
        labels = list(q["criteria"])
        if "PELICAN-42" in labels:                       # isolation: only the state is visible
            hit = "PELICAN-42" in str(state)
            p = {l: (0.9 if (l == "PELICAN-42") == hit and l != "not_stated" else 0.05) for l in labels}
            if not hit:
                p = {l: (0.9 if l == "not_stated" else 0.05) for l in labels}
        else:                                            # order bias + set-dependent temperature
            temp = 1.0 + 0.4 * (len(labels) - 4)
            bonus = {labels[-1]: 0.6}                    # last option gets a small boost
            z = {l: (BASE.get(l, 0.0) + bonus.get(l, 0.0)) / temp for l in labels}
            m = sum(math.exp(v) for v in z.values())
            p = {l: math.exp(v) / m for l, v in z.items()}
        best = max(p, key=p.get)
        out[qid] = ChoiceResult(best, p, choice_confidence(list(p.values())))
    return out


def test_probes_detect_reported_behaviours() -> None:
    r = {x.name: x for x in run_all(FakeDecisionClient(mimic))}
    assert r["order_sensitivity"].value > 0
    assert r["sibling_isolation"].verdict.startswith("ok")
    assert r["irrelevant_option"].verdict == "options interact"


def test_confidence_formula() -> None:
    assert abs(choice_confidence([0.8, 0.1, 0.1]) - 0.7) < 1e-9
    assert choice_confidence([1.0]) == 1.0 and choice_confidence([0.25] * 4) == 0.0
