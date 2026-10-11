# mini-projects/04-chief-of-staff-router/chief.py
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from queue_manager import enqueue
from triage.jev_client import DecisionError, client_from_env

QUESTIONS: Final = {
    "worker": {"type": "choice", "instructions": "Who should handle `goal` next?",
               "criteria": {"research": "Needs finding or validating external evidence before anything is written",
                            "write": "Needs drafting, rewriting or summarising text from evidence already in hand",
                            "tool": "One deterministic operation: a lookup, a file move, an API call",
                            "review": "Sensitive, irreversible, or unclear; a person must look first"}},
    "complexity": {"type": "score", "instructions": "How complex is the next step for `goal`?",
                   "criteria": ["Mechanical", "Multi-step", "Ambiguous or high-risk"]},
    "needs_approval": {"type": "noul",
                       "instructions": "Would the next step for `goal` send, publish, pay, or delete something?"},
}
MIN_CONF: Final = {"research": 0.70, "write": 0.70, "tool": 0.90, "review": 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description="Route a goal to a worker queue via Jev")
    ap.add_argument("goal")
    ap.add_argument("--queue-root", type=Path, default=Path("queue"))
    args = ap.parse_args()
    state = {"goal": args.goal.strip(), "available_workers": list(MIN_CONF)}
    try:
        r = client_from_env().decide(state, QUESTIONS)
    except DecisionError as e:
        path = enqueue(args.queue_root, "review", {"state": state, "error": str(e)})
        print(f"decision unavailable -> review: {path}", file=sys.stderr)
        return 2
    worker, approval = r.choice("worker"), r.noul("needs_approval")
    dest = worker.choice
    if approval.p_yes >= 0.70 or worker.confidence < MIN_CONF[dest] or r.score("complexity").score > 1.5:
        dest = "review"
    job = {"created_at": datetime.now(timezone.utc).isoformat(), "model": r.model, "state": state,
           "decision": {"worker": worker.choice, "probabilities": dict(worker.probabilities),
                        "confidence": worker.confidence, "complexity": r.score("complexity").score,
                        "needs_approval": approval.p_yes},
           "destination": dest, "latency_ms": round(r.latency_ms, 1)}
    print(f"queued for {dest}: {enqueue(args.queue_root, dest, job)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())