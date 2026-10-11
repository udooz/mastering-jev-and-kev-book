"""Fault injection for the motor simulator: degradation, spikes, and bad sensors."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

CHANNELS: Final[tuple[str, ...]] = ("vibration_rms_mm_s", "bearing_temp_c", "current_a", "rpm")


class FaultMode(str, Enum):
    GRADUAL_DEGRADATION = "gradual_degradation"   # bearing wear: vibration and temperature creep up
    THERMAL_SPIKE = "thermal_spike"               # sudden temperature rise (lubrication loss, blocked cooling)
    SENSOR_DROPOUT = "sensor_dropout"             # channel stops reporting (None)
    SENSOR_NOISE = "sensor_noise"                 # channel becomes very noisy
    STUCK_SENSOR = "stuck_sensor"                 # channel freezes at its last value
    CONFLICTING_TEMP_RISE = "conflicting_temp_rise"  # temperature rises, vibration stays normal


@dataclass(frozen=True, slots=True)
class Fault:
    mode: FaultMode
    start_step: int
    duration_steps: int | None = None   # None = until the end of the run
    magnitude: float = 1.0
    channel: str = "bearing_temp_c"     # used by sensor faults

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        if self.start_step < 0 or self.magnitude < 0:
            raise ValueError("start_step and magnitude must be non-negative")

    def active(self, step: int) -> bool:
        if step < self.start_step:
            return False
        return self.duration_steps is None or step < self.start_step + self.duration_steps


@dataclass(slots=True)
class FaultInjector:
    faults: tuple[Fault, ...]
    rng: random.Random
    _stuck_values: dict[int, float | None] = field(default_factory=dict)

    def apply(self, values: dict[str, float | None], step: int) -> dict[str, float | None]:
        out = dict(values)
        for i, f in enumerate(self.faults):
            if not f.active(step):
                continue
            k = step - f.start_step          # steps since fault onset
            m = f.magnitude
            if f.mode is FaultMode.GRADUAL_DEGRADATION:
                out["vibration_rms_mm_s"] = _add(out["vibration_rms_mm_s"], 0.06 * m * k)
                out["bearing_temp_c"] = _add(out["bearing_temp_c"], min(30.0, 0.12 * m * k))
                out["current_a"] = _add(out["current_a"], 0.02 * m * k)
            elif f.mode is FaultMode.THERMAL_SPIKE:
                out["bearing_temp_c"] = _add(out["bearing_temp_c"], 28.0 * m * (1 - math.exp(-k / 3)))
            elif f.mode is FaultMode.CONFLICTING_TEMP_RISE:
                out["bearing_temp_c"] = _add(out["bearing_temp_c"], min(25.0, 0.2 * m * k))
            elif f.mode is FaultMode.SENSOR_DROPOUT:
                out[f.channel] = None
            elif f.mode is FaultMode.SENSOR_NOISE:
                out[f.channel] = _add(out[f.channel], self.rng.gauss(0.0, 6.0 * m))
            elif f.mode is FaultMode.STUCK_SENSOR:
                if i not in self._stuck_values:
                    self._stuck_values[i] = out[f.channel]
                out[f.channel] = self._stuck_values[i]
        return out


def _add(value: float | None, delta: float) -> float | None:
    return None if value is None else value + delta
