# mini-projects/06-retrieval-reranker/bm25_search.py
"""Stage 1: pure-Python BM25 over a small corpus. Fast, deterministic, recall-oriented.

  uv run python mini-projects/06-retrieval-reranker/bm25_search.py "money debited but UPI payment failed"
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

_TOKEN = re.compile(r"[a-z0-9]+")
# DEFAULT_CORPUS = Path(__file__).with_name("policy_clauses.json")
DEFAULT_CORPUS = Path(__file__).parent / "policy_clauses.json"


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True, slots=True)
class Hit:
    doc_id: int
    score: float


class BM25:
    def __init__(self, docs: Sequence[str], k1: float = 1.5, b: float = 0.75) -> None:
        if not docs:
            raise ValueError("empty corpus")
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(d)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = sum(self.len) / len(self.len)
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 30) -> list[Hit]:
        q = tokenize(query)
        scores: list[Hit] = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                scores.append(Hit(i, s))
        return sorted(scores, key=lambda h: h.score, reverse=True)[:k]


def load_corpus(path: Path = DEFAULT_CORPUS) -> list[dict[str, Any]]:
    """JSON list of {"id", "title", "text"}; the searchable document is title + text."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not rows or not all({"id", "text"} <= r.keys() for r in rows):
        raise ValueError(f"{path}: expected a non-empty list of objects with 'id' and 'text'")
    return rows


def as_documents(rows: Sequence[dict[str, Any]]) -> list[str]:
    return [f"{r.get('title', '')}. {r['text']}" for r in rows]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BM25 search over a JSON corpus")
    ap.add_argument("query")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args(argv)
    corpus_path = Path(__file__).parent / args.corpus
    rows = load_corpus(corpus_path)
    hits = BM25(as_documents(rows)).search(args.query, k=args.k)
    if not hits:
        print("no matching documents")
        return 1
    for rank, h in enumerate(hits, 1):
        print(f"{rank:2}. {rows[h.doc_id]['id']}  bm25={h.score:5.2f}  {rows[h.doc_id].get('title', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
