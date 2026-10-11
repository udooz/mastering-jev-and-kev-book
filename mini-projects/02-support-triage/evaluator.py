# mini-projects/02-support-triage/evaluator.py
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from triage.jev_client import DecisionError, client_from_env
from triage_schema import TICKET_QUESTIONS


@dataclass(frozen=True, slots=True)
class TicketVerdict:
    ticket_id: str
    department: str
    department_conf: float
    severity: float
    refund_p: float
    needs_human: bool
    latency_ms: float


def evaluate(ticket_id: str, text: str) -> TicketVerdict:
    client = client_from_env()
    try:
        r = client.decide({"ticket": text}, TICKET_QUESTIONS)
    except DecisionError as e:
        # Degrade safely: never drop a ticket because the decision layer is down.
        print(f"[{ticket_id}] decision failed ({e}); routing to human", file=sys.stderr)
        return TicketVerdict(ticket_id, "other", 0.0, -1.0, 0.0, True, 0.0)
    dept, sev, refund = r.choice("department"), r.score("severity"), r.noul("refund_requested")
    return TicketVerdict(
        ticket_id=ticket_id,
        department=dept.choice,
        department_conf=dept.confidence,
        severity=sev.score,
        refund_p=refund.p_yes,
        needs_human=dept.confidence < 0.6 or dept.choice == "other",
        latency_ms=r.latency_ms,
    )

if __name__ == "__main__":
    tickets_path = Path(__file__).parent / "sample_tickets.json"
    tickets = json.loads(tickets_path.read_text(encoding="utf-8"))
    for t in tickets:
        print(json.dumps(asdict(evaluate(t["id"], t["text"]))))