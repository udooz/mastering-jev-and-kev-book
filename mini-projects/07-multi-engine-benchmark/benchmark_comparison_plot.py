# mini-projects/07-multi-engine-benchmark/benchmark_comparison_plot.py
"""Plot benchmark_results.json (written by multi_engine_eval.py). Needs: uv sync --group ch7

  uv run python mini-projects/07-multi-engine-benchmark/benchmark_comparison_plot.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt


def main(path: str | Path = Path(__file__).with_name("benchmark_results.json")) -> None:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for r in rows:
        ax.scatter(max(r["p95_ms"], 1.0), r["accuracy"] * 100, s=60 + 4000 * r["usd_per_1k"], alpha=0.7)
        ax.annotate(f"{r['engine']}\nECE {r['ece']:.2f}", (max(r["p95_ms"], 1.0), r["accuracy"] * 100),
                    textcoords="offset points", xytext=(8, 4), fontsize=8)
    ax.set_xscale("log")
    ax.set_xlabel("p95 latency (ms, log scale)")
    ax.set_ylabel("Acceptable-route accuracy (%)")
    ax.set_title("Triage contract: engines on the same labeled set (bubble = cost/1k)")
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    out = Path(path).with_name("benchmark_comparison.png")
    fig.savefig(out, dpi=160)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
