# capstone-project/tools/sweep_threshold.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class Row:
    predicted: str
    confidence: float
    expected: str


@dataclass(frozen=True, slots=True)
class SweepPoint:
    threshold: float
    automation_share: float   # fraction auto-routed
    precision: float          # accuracy among auto-routed
    auto_errors: int


def sweep(rows: Sequence[Row], target: str, grid: Sequence[float]) -> list[SweepPoint]:
    rel = [r for r in rows if r.predicted == target]
    out: list[SweepPoint] = []
    for th in grid:
        auto = [r for r in rel if r.confidence >= th]
        correct = sum(r.predicted == r.expected for r in auto)
        out.append(SweepPoint(th, len(auto) / max(1, len(rel)),
                              correct / len(auto) if auto else 1.0, len(auto) - correct))
    return out


def pick(points: Sequence[SweepPoint], min_precision: float) -> float | None:
    ok = [p for p in points if p.precision >= min_precision and p.automation_share > 0]
    return min(ok, key=lambda p: p.threshold).threshold if ok else None


if __name__ == "__main__":
    import json, sys
    data = [Row(**json.loads(l)) for l in open(sys.argv[1], encoding="utf-8")]
    pts = sweep(data, "refund_pipeline", [x / 100 for x in range(50, 100, 5)])
    for p in pts:
        print(f"th={p.threshold:.2f} auto={p.automation_share:5.1%} precision={p.precision:6.1%} errors={p.auto_errors}")
    print("chosen:", pick(pts, min_precision=0.99))
