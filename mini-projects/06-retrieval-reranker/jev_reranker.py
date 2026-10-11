# mini-projects/06-retrieval-reranker/jev_reranker.py
"""Stage 2: re-rank a BM25 shortlist with one Noul per (case, passage) pair, in one call.

  uv run python mini-projects/06-retrieval-reranker/jev_reranker.py "UPI payment failed but money debited"
  uv run python mini-projects/06-retrieval-reranker/jev_reranker.py --eval             # top-k vs BM25 alone
  uv run python mini-projects/06-retrieval-reranker/jev_reranker.py --eval --offline   # no API key needed
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from bm25_search import BM25, as_documents, load_corpus, tokenize
from triage.jev_client import Answer, DecisionClient, DecisionError, FakeDecisionClient, NoulResult, client_from_env

DEFAULT_EVAL = Path(__file__).parent / "eval_cases.jsonl"
# DEFAULT_EVAL = Path(__file__).with_name("eval_cases.jsonl")


@dataclass(frozen=True, slots=True)
class Ranked:
    doc_id: int
    p_relevant: float
    bm25: float


def rerank(client: DecisionClient, query: str, docs: Sequence[str], index: BM25,
           shortlist: int = 30, min_p: float = 0.5) -> tuple[list[Ranked], float]:
    hits = index.search(query, k=shortlist)
    if not hits:
        return [], 0.0
    passages = [docs[h.doc_id][:1_500] for h in hits]
    qs = {f"p{i}": {"type": "noul",
                    "instructions": f"Does `passages[{i}]` state the rule that decides `case`?"}
          for i in range(len(passages))}
    t0 = time.perf_counter()
    try:
        r = client.decide({"case": query, "passages": passages}, qs)
    except DecisionError:
        # Degrade to stage-1 order; never return nothing because stage 2 failed.
        return [Ranked(h.doc_id, 0.0, h.score) for h in hits], 0.0
    ms = (time.perf_counter() - t0) * 1000
    ranked = sorted((Ranked(h.doc_id, r.noul(f"p{i}").p_yes, h.score) for i, h in enumerate(hits)),
                    key=lambda x: (x.p_relevant, x.bm25), reverse=True)
    return [x for x in ranked if x.p_relevant >= min_p] or ranked[:1], ms


def top_k_accuracy(results: Sequence[Sequence[int]], gold: Sequence[int], k: int) -> float:
    return sum(g in r[:k] for r, g in zip(results, gold, strict=True)) / len(gold)


# ---- Offline stand-in: rewards passages whose TITLE terms appear in the case. Not a model. ----
_STOP = {"the", "a", "an", "and", "or", "of", "to", "my", "i", "is", "it", "for", "on", "in", "was", "but"}


def offline_model(state: Any, questions: Mapping[str, Any]) -> Mapping[str, Answer]:
    case = {w for w in tokenize(state["case"]) if w not in _STOP}
    out: dict[str, Answer] = {}
    for qid in questions:
        passage = state["passages"][int(qid[1:])]
        title = {w for w in tokenize(passage.split(".", 1)[0]) if w not in _STOP}
        body = {w for w in tokenize(passage) if w not in _STOP}
        score = 0.6 * len(case & title) / max(1, len(title)) + 0.4 * len(case & body) / max(1, len(case))
        out[qid] = NoulResult(round(min(0.97, 0.05 + score), 3))
    return out


def _evaluate(client: DecisionClient, rows: list[dict[str, Any]], cases: list[dict[str, str]],
              shortlist: int) -> int:
    docs = as_documents(rows)
    index = BM25(docs)
    ids = [r["id"] for r in rows]
    bm25_runs, jev_runs, gold, total_ms = [], [], [], 0.0
    for c in cases:
        bm25_runs.append([h.doc_id for h in index.search(c["case"], k=shortlist)])
        ranked, ms = rerank(client, c["case"], docs, index, shortlist=shortlist, min_p=0.0)
        jev_runs.append([x.doc_id for x in ranked])
        gold.append(ids.index(c["gold"]))
        total_ms += ms
        top_b = ids[bm25_runs[-1][0]] if bm25_runs[-1] else "-"
        top_j = ids[jev_runs[-1][0]] if jev_runs[-1] else "-"
        mark = "ok " if top_j == c["gold"] else "MISS"
        print(f"{mark} gold={c['gold']}  bm25@1={top_b}  rerank@1={top_j}  {c['case'][:60]}")
    print(f"\n{'':10}{'Top-1':>8}{'Top-3':>8}")
    for name, runs in (("BM25", bm25_runs), ("re-ranked", jev_runs)):
        print(f"{name:10}{top_k_accuracy(runs, gold, 1):8.0%}{top_k_accuracy(runs, gold, 3):8.0%}")
    print(f"\nstage-2 decision time: {total_ms:.0f} ms over {len(cases)} cases")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Two-stage retrieval: BM25 shortlist -> System One re-rank")
    ap.add_argument("query", nargs="?", help="a case to find the deciding clause for")
    default_corpus_path = Path(__file__).parent / "policy_clauses.json"
    ap.add_argument("--corpus", type=Path, default=default_corpus_path)
    ap.add_argument("--eval", nargs="?", const=DEFAULT_EVAL, type=Path,
                    help="JSONL of {case, gold} to compare BM25 vs re-ranked accuracy")
    ap.add_argument("--shortlist", type=int, default=8)
    ap.add_argument("--min-p", type=float, default=0.5)
    ap.add_argument("--offline", action="store_true", help="use the keyword stand-in instead of Jev/Kev")
    args = ap.parse_args(argv)
    if not args.query and not args.eval:
        ap.error("give a query or --eval")

    rows = load_corpus(args.corpus)
    client: DecisionClient = FakeDecisionClient(offline_model) if args.offline else client_from_env()
    if args.eval:
        cases = [json.loads(l) for l in args.eval.read_text(encoding="utf-8").splitlines() if l.strip()]
        return _evaluate(client, rows, cases, args.shortlist)

    docs = as_documents(rows)
    ranked, ms = rerank(client, args.query, docs, BM25(docs), shortlist=args.shortlist, min_p=args.min_p)
    if not ranked:
        print("no candidate passages")
        return 1
    for rank, x in enumerate(ranked, 1):
        print(f"{rank:2}. {rows[x.doc_id]['id']}  p={x.p_relevant:.2f}  bm25={x.bm25:5.2f}  {rows[x.doc_id]['title']}")
    print(f"\nstage-2 decision time: {ms:.0f} ms ({client.backend})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
