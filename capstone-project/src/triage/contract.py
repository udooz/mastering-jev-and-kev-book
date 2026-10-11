# capstone-project/src/triage/contract.py
"""The triage decision contract. Every question and label lives here, reviewed as one file."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Final, Literal, Mapping

CONTRACT_VERSION: Final[str] = "triage-contract/1.0.0"

DispatchTarget = Literal["tier1_support", "secops_agent", "refund_pipeline", "manual_escalation"]
DISPATCH_TARGETS: Final[tuple[DispatchTarget, ...]] = (
    "tier1_support", "secops_agent", "refund_pipeline", "manual_escalation",
)

URGENCY_LEVELS: Final[list[dict[str, Any]]] = [
    {"what": "Informational; no money or access at risk",
     "examples": ["how do I download last month's statement?"]},
    {"what": "Inconvenience; a feature is degraded but money and access are safe",
     "examples": ["UPI payment page loads slowly"]},
    {"what": "Customer is blocked from a normal action but no loss has occurred",
     "examples": ["card declined at checkout, balance is fine"]},
    {"what": "A loss has already occurred or is disputed, and it is not ongoing",
     "examples": ["I was charged twice for the same order last week"]},
    {"what": "Money or account access is being lost right now, or a takeover is in progress",
     "examples": ["OTPs I didn't request, and money is leaving my account"]},
]

TRIAGE_QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    "is_fraud_suspected": {
        "type": "noul",
        "instructions": (
            "Does `report.body` describe a transaction, login or account change that the "
            "customer says they did not authorise?"
        ),
        "criteria": {
            "true": "Customer disowns a payment, login, device, OTP, or account change",
            "false": "Customer recognises all activity; the issue is service, fees or errors",
        },
    },
    "urgency_rating": {
        "type": "score",
        "instructions": "How urgent is the situation described in `report.body`?",
        "criteria": URGENCY_LEVELS,
    },
    "dispatch_target": {
        "type": "choice",
        "instructions": "Which team should own the next step for `report`?",
        "criteria": {
            "tier1_support": "How-to questions, app errors, slow features; no disputed money",
            "secops_agent": "Unauthorised access, account takeover, phishing, suspicious logins or OTPs",
            "refund_pipeline": "Customer recognises the merchant but disputes an amount: "
                               "duplicate charge, failed-but-debited, refund not received",
            "manual_escalation": "Legal threats, regulator complaints, bereavement, or anything "
                                 "that fits none of the other teams",
        },
    },
}


class ContractError(ValueError):
    pass


def validate_contract(questions: Mapping[str, Mapping[str, Any]]) -> None:
    """Lint the contract at import time: fail the build, not production."""
    for qid, q in questions.items():
        kind = q.get("type")
        if not str(q.get("instructions", "")).strip():
            raise ContractError(f"{qid}: instructions are required (the ID is never sent)")
        if kind == "choice":
            crit = q.get("criteria") or {}
            if not 2 <= len(crit) <= 255:
                raise ContractError(f"{qid}: Choice needs 2..255 options, got {len(crit)}")
        elif kind == "score":
            levels = q.get("criteria") or []
            if not 2 <= len(levels) <= 10:
                raise ContractError(f"{qid}: Score needs 2..10 levels, got {len(levels)}")
            shapes = {tuple(sorted(l)) if isinstance(l, dict) else "str" for l in levels}
            if len(shapes) > 1:
                raise ContractError(f"{qid}: every level must use the same shape/field names")
        elif kind != "noul":
            raise ContractError(f"{qid}: unknown type {kind!r}")
    if set(questions["dispatch_target"]["criteria"]) != set(DISPATCH_TARGETS):
        raise ContractError("dispatch_target options drifted from DISPATCH_TARGETS")


def contract_hash(questions: Mapping[str, Any] = TRIAGE_QUESTIONS) -> str:
    """Stable content hash: log it with every decision trace."""
    blob = json.dumps(questions, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def to_business_urgency(raw_score: float) -> int:
    """Jev returns 0..4 (probability-weighted). The business scale is 1..5 integers."""
    if not 0.0 <= raw_score <= 4.0:
        raise ContractError(f"urgency score out of range: {raw_score}")
    return int(min(5, max(1, round(raw_score) + 1)))


validate_contract(TRIAGE_QUESTIONS)
