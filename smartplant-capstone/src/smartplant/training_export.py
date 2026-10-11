"""Turn the audit trail into Kev fine-tuning data: snapshot + contract + expert-confirmed labels.

Output is one JSON object per line in kev.train's format: each question carries a `label`
(choice -> option name, noul -> true/false, score -> zero-based level).
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from smartplant.contract import ACTIONS, URGENCIES, build_questions

LABEL_KEYS = ("intervention_urgency", "operational_action", "standard_workflow", "operational_severity")


def _labeled_example(state: Mapping[str, Any], labels: Mapping[str, Any]) -> dict[str, Any] | None:
    if not all(k in labels for k in LABEL_KEYS):
        return None                                    # partial labels are not training data
    qs = build_questions(state, speculative=False)
    if labels["operational_action"] not in qs["operational_action"]["criteria"] or \
            labels["intervention_urgency"] not in URGENCIES or labels["operational_action"] not in ACTIONS:
        return None
    sev = int(labels["operational_severity"])
    if not 1 <= sev <= 5:
        return None
    qs["intervention_urgency"]["label"] = labels["intervention_urgency"]
    qs["operational_action"]["label"] = labels["operational_action"]
    qs["standard_workflow"]["label"] = bool(labels["standard_workflow"])
    qs["operational_severity"]["label"] = sev - 1          # plant scale 1..5 -> zero-based level
    return {"state": dict(state), "questions": qs}


def examples_from_audit(lines: Iterable[str]) -> list[dict[str, Any]]:
    by_corr: dict[str, dict[str, Any]] = defaultdict(dict)
    for line in lines:
        if not line.strip():
            continue
        e = json.loads(line)
        if e["event"] in ("snapshot", "outcome"):
            by_corr[e["correlation_id"]][e["event"]] = e["payload"]
    out = []
    for parts in by_corr.values():
        if "snapshot" in parts and "outcome" in parts:
            ex = _labeled_example(parts["snapshot"], parts["outcome"].get("expert_labels", {}))
            if ex:
                out.append(ex)
    return out


def write_split(examples: list[dict[str, Any]], out_dir: Path, heldout: float = 0.15, seed: int = 7) -> tuple[Path, Path]:
    rnd = random.Random(seed)
    rows = examples[:]
    rnd.shuffle(rows)
    k = max(1, int(len(rows) * heldout)) if len(rows) > 1 else 0
    out_dir.mkdir(parents=True, exist_ok=True)
    train, held = out_dir / "train.jsonl", out_dir / "heldout.jsonl"
    train.write_text("".join(json.dumps(r) + "\n" for r in rows[k:]), encoding="utf-8")
    held.write_text("".join(json.dumps(r) + "\n" for r in rows[:k]), encoding="utf-8")
    return train, held


def main() -> int:
    ap = argparse.ArgumentParser(description="Export expert-labeled decisions for kev.train")
    ap.add_argument("audit", type=Path, nargs="+")
    ap.add_argument("--out", type=Path, default=Path("kev-finetune"))
    args = ap.parse_args()
    ex = [x for p in args.audit for x in examples_from_audit(p.read_text(encoding="utf-8").splitlines())]
    train, held = write_split(ex, args.out)
    print(f"{len(ex)} labeled examples -> {train}, {held}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
