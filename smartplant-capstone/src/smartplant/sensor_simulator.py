"""Deterministic motor telemetry simulator (vibration, bearing temperature, current, RPM).

Seeded and pure Python: the same seed and faults always produce the same stream, so a
scenario that broke last week can be replayed exactly. It does not control machinery.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from smartplant.fault_injector import Fault, FaultInjector, FaultMode


@dataclass(frozen=True, slots=True)
class Reading:
    asset_id: str
    seq: int
    ts: datetime
    vibration_rms_mm_s: float | None
    bearing_temp_c: float | None
    current_a: float | None
    rpm: float | None

    def to_json(self) -> str:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return json.dumps(d)


@dataclass(frozen=True, slots=True)
class AssetProfile:
    asset_id: str = "M-104"
    rated_rpm: float = 1480.0
    rated_current_a: float = 42.0
    base_vibration_mm_s: float = 1.8
    base_bearing_temp_c: float = 62.0
    ambient_c: float = 30.0


LoadProfile = Callable[[int], float]   # step -> load fraction 0..1


@dataclass(slots=True)
class SensorSimulator:
    profile: AssetProfile = field(default_factory=AssetProfile)
    seed: int = 7
    interval_s: float = 10.0
    start: datetime = field(default_factory=lambda: datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc))
    load: LoadProfile = lambda _step: 0.75
    faults: Sequence[Fault] = ()
    _rng: random.Random = field(init=False)
    _injector: FaultInjector = field(init=False)
    _step: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._injector = FaultInjector(tuple(self.faults), random.Random(self.seed + 1))

    def _healthy(self, load: float) -> dict[str, float | None]:
        p, g = self.profile, self._rng.gauss
        return {
            "vibration_rms_mm_s": max(0.1, p.base_vibration_mm_s * (0.8 + 0.4 * load) + g(0, 0.08)),
            "bearing_temp_c": p.ambient_c + (p.base_bearing_temp_c - p.ambient_c) * (0.6 + 0.5 * load) + g(0, 0.3),
            "current_a": p.rated_current_a * (0.3 + 0.75 * load) + g(0, 0.25),
            "rpm": p.rated_rpm * (1 - 0.015 * load) + g(0, 1.5),
        }

    def step(self) -> Reading:
        load = min(1.0, max(0.0, self.load(self._step)))
        values = self._injector.apply(self._healthy(load), self._step)
        r = Reading(
            asset_id=self.profile.asset_id,
            seq=self._step,
            ts=self.start + timedelta(seconds=self.interval_s * self._step),
            **{k: (None if v is None else round(v, 3)) for k, v in values.items()},
        )
        self._step += 1
        return r

    def stream(self, n: int) -> Iterator[Reading]:
        for _ in range(n):
            yield self.step()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Emit simulated motor telemetry as JSON lines")
    ap.add_argument("--asset", default="M-104")
    ap.add_argument("--steps", type=int, default=120)
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between samples")
    ap.add_argument("--load", type=float, default=0.75)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fault", choices=[m.value for m in FaultMode], action="append", default=[])
    ap.add_argument("--fault-start", type=int, default=30)
    ap.add_argument("--channel", default="bearing_temp_c")
    args = ap.parse_args(argv)
    faults = [Fault(FaultMode(m), args.fault_start, channel=args.channel) for m in args.fault]
    sim = SensorSimulator(AssetProfile(asset_id=args.asset), seed=args.seed, interval_s=args.interval,
                          load=lambda _s: args.load, faults=faults)
    for r in sim.stream(args.steps):
        sys.stdout.write(r.to_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
