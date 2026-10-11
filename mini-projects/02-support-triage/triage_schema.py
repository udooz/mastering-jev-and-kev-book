from __future__ import annotations
from typing import Any, Final

TICKET_QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    "department": {
        "type": "choice",
        "instructions": "Which team should handle `ticket`?",
        "criteria": {
            "billing": "Charges, invoices, refunds, subscriptions",
            "technical": "Bugs, outages, integration problems",
            "sales": "Pricing questions, upgrades, new accounts",
            "other": None,
        },
    },
    "severity": {
        "type": "score",
        "instructions": "If `ticket` reports a problem, how severe is it?",
        "criteria": [
            "Cosmetic; no impact on functionality",
            "Broken or degraded feature, but a workaround exists",
            "Blocking issue; no workaround exists",
        ],
    },
    "refund_requested": {
        "type": "noul",
        "instructions": "Does `ticket` ask for money back?",
    },
}