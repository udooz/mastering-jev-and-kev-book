# mini-projects/03-lead-scoring-pipeline/composite_scorer.py
"""Speculative lead qualification: one call, five questions, weights you own.

  uv run python mini-projects/03-lead-scoring-pipeline/composite_scorer.py --offline      # demo, no key
  uv run python mini-projects/03-lead-scoring-pipeline/composite_scorer.py --leads my_leads.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Sequence

from triage.jev_client import (Answer, ChoiceResult, DecisionClient, DecisionError, DecisionResponse,
                               FakeDecisionClient, NoulResult, ScoreResult, client_from_env)

Tier = Literal["hot", "warm", "nurture", "drop"]

LEAD_QUESTIONS: Final[dict[str, dict]] = {
    "intent": {"type": "choice", "instructions": "What does the sender of `email.body` want?",
               "criteria": {"buy": "Asks for pricing, a quote, a trial or a sales call",
                            "support": "Existing customer with a problem",
                            "partnership": "Proposes integration, reselling or co-marketing",
                            "other": None}},
    "fit": {"type": "score", "instructions": "How well does `company` match `ideal_customer`?",
            "criteria": ["Outside target industry and size",
                         "Right industry or right size, not both",
                         "Right industry and size",
                         "Right industry, size, and names a use case we serve"]},
    "timing": {"type": "score", "instructions": "When does `email.body` say they want to start?",
               "criteria": ["No timeframe stated", "This year", "This quarter", "This month or sooner"]},
    "frustration": {"type": "score", "instructions": "How frustrated is the author of `email.body`?",
                    "criteria": ["Calm", "Frustrated but civil", "Angry or threatening to leave"]},
    "is_spam": {"type": "noul", "instructions": "Is `email.body` a mass-mailed template or cold pitch?"},
}

WEIGHTS: Final[Mapping[str, float]] = {"fit": 0.5, "timing": 0.35, "frustration": 0.15}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "weights must sum to 1"
IDEAL_CUSTOMER: Final = "Fintech or payments companies, 100-2000 employees, routing or fraud decisions"
DEFAULT_LEADS: Final = Path(__file__).parent / "sample_leads.json"
_SIGNATURE = re.compile(r"(?s)(\n--\s*\n.*|\nsent from my \w+.*)$", re.I)

@dataclass(frozen=True, slots=True)
class LeadDecision:
    tier: Tier
    composite: float
    reason: str


def score_lead(r: DecisionResponse) -> LeadDecision:
    if r.noul("is_spam").p_yes > 0.8:
        return LeadDecision("drop", 0.0, "spam")
    intent = r.choice("intent")
    if intent.confidence < 0.5:
        return LeadDecision("nurture", 0.0, "unclear intent -> human skim")
    composite = sum(w * r.score(q).normalized() for q, w in WEIGHTS.items())
    if intent.choice != "buy":
        return LeadDecision("nurture", composite, f"intent={intent.choice}")
    tier: Tier = "hot" if composite >= 0.7 else "warm" if composite >= 0.4 else "nurture"
    return LeadDecision(tier, round(composite, 3), "weighted fit/timing/frustration")


def build_lead_state(lead: Mapping[str, Any]) -> dict[str, Any]:
    """Code computes the facts (signature stripped, size bucketed); Jev judges the meaning."""
    employees = int(lead.get("company", {}).get("employees", 0))
    band = ("under_50" if employees < 50 else "50_200" if employees < 200
            else "200_2000" if employees <= 2000 else "over_2000")
    return {
        "email": {"body": _SIGNATURE.sub("", str(lead["body"])).strip()[:2000]},
        "company": {"industry": lead.get("company", {}).get("industry", "unknown"), "employees_band": band},
        "ideal_customer": IDEAL_CUSTOMER,
    }


def offline_model(state: Any, questions: Mapping[str, Any]) -> Mapping[str, Answer]:
    """OFFLINE STAND-IN: keyword rules so the pipeline runs without a key. Not a model."""
    body = state["email"]["body"].lower()
    spam = bool(re.search(r"dear sir|reply yes|free audit|rank your website|!!!", body))
    if re.search(r"quote|pricing|trial|demo|sales call", body):
        intent = "buy"
    elif re.search(r"down|error|broken|cancel|not working", body):
        intent = "support"
    elif re.search(r"partner|resell|integrat", body):
        intent = "partnership"
    else:
        intent = "other"
    fintech = state["company"]["industry"] in ("fintech", "payments")
    sized = state["company"]["employees_band"] in ("50_200", "200_2000")
    fit = 2.0 * fintech + 1.0 * sized
    timing = 3.0 if "this month" in body or "asap" in body else 2.0 if "this quarter" in body \
        else 1.0 if "this year" in body else 0.0
    anger = 2.0 if re.search(r"or we cancel|third time|unacceptable", body) else \
        1.0 if re.search(r"frustrat|again|still", body) else 0.0
    labels = list(questions["intent"]["criteria"])
    probs = {l: (0.8 if l == intent else 0.2 / (len(labels) - 1)) for l in labels}
    return {
        "intent": ChoiceResult(intent, probs, 0.73),
        "fit": ScoreResult(min(3.0, fit), {}, 0.7, 4),
        "timing": ScoreResult(timing, {}, 0.7, 4),
        "frustration": ScoreResult(anger, {}, 0.7, 3),
        "is_spam": NoulResult(0.95 if spam else 0.05),
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qualify inbound leads with one System One call each")
    ap.add_argument("--leads", type=Path, default=DEFAULT_LEADS, help="JSON list of {id, body, company}")
    ap.add_argument("--offline", action="store_true", help="use the keyword stand-in instead of Jev/Kev")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    leads_path = Path(__file__).parent / args.leads
    leads = json.loads(leads_path.read_text(encoding="utf-8"))
    client: DecisionClient = FakeDecisionClient(offline_model) if args.offline else client_from_env()
    counts: dict[str, int] = {}
    for lead in leads:
        state = build_lead_state(lead)
        try:
            r = client.decide(state, LEAD_QUESTIONS)
            d = score_lead(r)
        except DecisionError as e:
            d = LeadDecision("nurture", 0.0, f"decision unavailable ({e}) -> human skim")
        counts[d.tier] = counts.get(d.tier, 0) + 1
        if args.json:
            print(json.dumps({"id": lead.get("id"), **asdict(d)}))
        else:
            print(f"{lead.get('id', '?'):4} {d.tier.upper():8} {d.composite:5.2f}  {d.reason:34}  {lead['body'][:48]!r}")
    if not args.json:
        print("\n" + "  ".join(f"{t}={n}" for t, n in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())