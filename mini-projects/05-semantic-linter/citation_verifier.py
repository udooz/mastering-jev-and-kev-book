# mini-projects/05-semantic-linter/citation_verifier.py
"""Citation verifier: an LLM writes the summary, Jev/Kev checks every claim against the transcript.

  uv run python mini-projects/05-semantic-linter/citation_verifier.py --offline                 # built-in demo
  uv run python mini-projects/05-semantic-linter/citation_verifier.py \
      --transcript call_transcript.txt --claims summary_claims.txt

Exit code: 0 = every claim kept, 1 = at least one claim must be removed or reviewed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Sequence

from triage.jev_client import Answer, ChoiceResult, DecisionClient, FakeDecisionClient, client_from_env

Support = Literal["supports", "contradicts", "not_mentioned"]
CHUNK: Final[int] = 40      # claims per request; keep the shared state sent once
KEEP_MIN_CONFIDENCE: Final[float] = 0.8

DEMO_TRANSCRIPT: Final = (
    "Agent: Thanks for calling, how can I help? "
    "Customer: I was charged twice for order A-104, two payments of 499 rupees on Monday. "
    "Agent: I can see both charges. The second one is a duplicate and I will raise a refund today. "
    "Customer: How long will it take? "
    "Agent: Refunds to UPI usually reach you within five working days."
)
DEMO_CLAIMS: Final = (
    "The customer was charged twice for order A-104.",
    "Each payment was 499 rupees.",
    "The agent raised a refund for the duplicate charge.",
    "The refund will arrive within five working days.",
    "The customer asked to close their account.",
    "The duplicate charge was 999 rupees.",
)


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    claim: str
    verdict: Support
    confidence: float

    @property
    def keep(self) -> bool:
        return self.verdict == "supports" and self.confidence >= KEEP_MIN_CONFIDENCE


def verify_claims(client: DecisionClient, transcript: str, claims: Sequence[str]) -> list[ClaimCheck]:
    out: list[ClaimCheck] = []
    for start in range(0, len(claims), CHUNK):
        batch = list(claims[start:start + CHUNK])
        qs = {f"c{i}": {"type": "choice",
                        "instructions": f"Compared with `transcript`, what is the status of `claims[{i}]`?",
                        "criteria": {"supports": "The transcript states this or directly implies it",
                                     "contradicts": "The transcript states something incompatible",
                                     "not_mentioned": "The transcript does not address this"}}
              for i in range(len(batch))}
        r = client.decide({"transcript": transcript, "claims": batch}, qs)
        out += [ClaimCheck(c, r.choice(f"c{i}").choice, r.choice(f"c{i}").confidence)  # type: ignore[arg-type]
                for i, c in enumerate(batch)]
    return out


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "was", "were", "is", "for", "to", "of", "and", "will", "their", "they", "it",
         "within", "each", "one", "two", "on", "in", "at", "be", "has", "had", "with"}


def offline_model(state: Any, questions: Mapping[str, Any]) -> Mapping[str, Answer]:
    """OFFLINE STAND-IN: word overlap + number mismatch. Measures the pipeline, not a model."""
    t_words = set(_WORD.findall(state["transcript"].lower()))
    t_nums = {w for w in t_words if w.isdigit()}
    out: dict[str, Answer] = {}
    for qid in questions:
        claim = state["claims"][int(qid[1:])].lower()
        words = [w for w in _WORD.findall(claim) if w not in _STOP]
        overlap = sum(w in t_words for w in words) / max(1, len(words))
        nums = {w for w in words if w.isdigit()}
        if nums and not nums <= t_nums and overlap >= 0.4:
            label, p = "contradicts", 0.75          # talks about the same thing, different number
        elif overlap >= 0.6:
            label, p = "supports", 0.9
        else:
            label, p = "not_mentioned", 0.8
        probs = {k: (p if k == label else (1 - p) / 2) for k in ("supports", "contradicts", "not_mentioned")}
        out[qid] = ChoiceResult(label, probs, (p - 1 / 3) / (2 / 3))
    return out


def _load_claims(path: str) -> list[str]:
    file_path = Path(__file__).parent / path
    text = file_path.read_text(encoding="utf-8")
    lines = [l.strip(" -*\t") for l in text.splitlines() if l.strip()]
    if len(lines) == 1:                                   # a paragraph: split into sentences
        lines = [s.strip() for s in re.split(r"(?<=[.!?])\s+", lines[0]) if s.strip()]
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check every summary claim against its source transcript")
    ap.add_argument("--transcript", help="source text file (default: built-in demo call)")
    ap.add_argument("--claims", help="one claim per line, or a paragraph to split into sentences")
    ap.add_argument("--offline", action="store_true", help="use the overlap stand-in instead of Jev/Kev")
    ap.add_argument("--json", action="store_true", help="print JSON lines instead of a table")
    args = ap.parse_args(argv)
    if bool(args.transcript) != bool(args.claims):
        ap.error("--transcript and --claims go together (omit both for the demo)")

    if args.transcript:
        transcript_path = Path(__file__).parent / args.transcript
        transcript = transcript_path.read_text(encoding="utf-8")
    else:
        transcript = DEMO_TRANSCRIPT

    claims = _load_claims(args.claims) if args.claims else list(DEMO_CLAIMS)
    client: DecisionClient = FakeDecisionClient(offline_model) if args.offline else client_from_env()
    checks = verify_claims(client, transcript, claims)

    for c in checks:
        if args.json:
            print(json.dumps({**asdict(c), "confidence": round(c.confidence, 3), "keep": c.keep}))
        else:
            print(f"{'KEEP' if c.keep else 'FLAG':4}  {c.verdict:13} {c.confidence:4.2f}  {c.claim}")
    flagged = sum(not c.keep for c in checks)
    if not args.json:
        print(f"\n{len(checks) - flagged}/{len(checks)} claims kept; {flagged} need removal or review")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
