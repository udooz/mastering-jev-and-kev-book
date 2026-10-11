"""Edge context builder: raw readings -> validated features -> compact, versioned snapshot.

Everything numeric happens HERE, in code: rolling statistics, slopes, staleness, plausibility,
and banding. Kev only ever sees named buckets plus a few rounded values for context.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from smartplant.sensor_simulator import AssetProfile, Reading

SNAPSHOT_SCHEMA_VERSION: Final[str] = "smartplant-snapshot/1.0.0"
CRITICAL_CHANNELS: Final[tuple[str, ...]] = ("vibration_rms_mm_s", "bearing_temp_c")
DataQuality = Literal["valid", "degraded", "invalid"]


@dataclass(frozen=True, slots=True)
class SensorLimits:
    """Illustrative per-asset limits. Real limits come from the OEM and your reliability engineers."""
    vib_zone_b: float = 2.3        # mm/s RMS, upper edge of zone A (good)
    vib_zone_c: float = 4.5        # upper edge of zone B (acceptable)
    vib_zone_d: float = 7.1        # upper edge of zone C (alert); above = D (danger)
    vib_trip: float = 11.0         # protective trip, enforced by the PLC
    temp_elevated: float = 75.0    # degC
    temp_high: float = 85.0
    temp_trip: float = 95.0        # protective trip, enforced by the PLC
    max_age_s: float = 60.0        # newest sample older than this -> stale
    min_samples: int = 6


@dataclass(frozen=True, slots=True)
class EdgeSnapshot:
    schema_version: str
    asset_id: str
    window_start: str
    window_end: str
    built_at: str
    samples: int
    latest: dict[str, float | None]
    features: dict[str, float | None]
    bands: dict[str, str]
    data_quality: DataQuality
    quality_issues: tuple[str, ...]
    protective_limit_reached: bool
    snapshot_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quality_issues"] = list(self.quality_issues)
        return d


def _slope_per_10min(points: list[tuple[datetime, float]]) -> float | None:
    if len(points) < 3:
        return None
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 600.0 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _noise_sigma(points: list[tuple[datetime, float]]) -> float:
    """Robust noise estimate from successive differences (MAD). Trends and single steps don't inflate it."""
    if len(points) < 6:
        return 0.0
    diffs = [abs(b[1] - a[1]) for a, b in zip(points, points[1:])]
    return 1.4826 * statistics.median(diffs) / 2 ** 0.5


@dataclass(slots=True)
class EdgeStateBuilder:
    profile: AssetProfile = field(default_factory=AssetProfile)
    limits: SensorLimits = field(default_factory=SensorLimits)
    window: timedelta = timedelta(minutes=20)
    _buf: deque[Reading] = field(default_factory=deque)
    _trip_latched: bool = False

    def reset_trip(self) -> None:
        """Operator reset after the PLC trip is cleared. Like the PLC, our mirror latches until then."""
        self._trip_latched = False

    def add(self, r: Reading) -> None:
        if r.asset_id != self.profile.asset_id:
            raise ValueError(f"reading for {r.asset_id} sent to builder for {self.profile.asset_id}")
        if self._buf and r.ts <= self._buf[-1].ts:
            return                                    # drop duplicates / out-of-order samples
        self._buf.append(r)
        while self._buf and r.ts - self._buf[0].ts > self.window:
            self._buf.popleft()

    def snapshot(self, now: datetime) -> EdgeSnapshot:
        L, buf = self.limits, list(self._buf)
        issues: list[str] = []
        if len(buf) < L.min_samples:
            issues.append("insufficient_samples")
        if buf and (now - buf[-1].ts).total_seconds() > L.max_age_s:
            issues.append("stale")

        series: dict[str, list[tuple[datetime, float]]] = {
            ch: [(r.ts, v) for r in buf if (v := getattr(r, ch)) is not None]
            for ch in ("vibration_rms_mm_s", "bearing_temp_c", "current_a", "rpm")
        }
        latest = {ch: (round(s[-1][1], 2) if s else None) for ch, s in series.items()}
        missing = {ch: 1 - len(s) / len(buf) if buf else 1.0 for ch, s in series.items()}
        slopes = {ch: _slope_per_10min(s) for ch, s in series.items()}

        for ch in CRITICAL_CHANNELS:
            if missing[ch] > 0.6:
                issues.append(f"missing:{ch}")
            elif missing[ch] > 0.3:
                issues.append(f"gaps:{ch}")
            tail = [v for _, v in series[ch][-12:]]
            if len(tail) >= 12 and statistics.pstdev(tail) < 1e-6:
                issues.append(f"stuck:{ch}")
            noise_limit = 2.0 if ch == "bearing_temp_c" else 0.8
            if _noise_sigma(series[ch]) > noise_limit:
                issues.append(f"noisy:{ch}")

        t, v, cur, rpm = (latest[c] for c in ("bearing_temp_c", "vibration_rms_mm_s", "current_a", "rpm"))
        if (t is not None and not -20 <= t <= 200) or (v is not None and not 0 <= v <= 50) or \
                (rpm is not None and cur is not None and rpm < 100 and cur > 10):
            issues.append("implausible_values")

        invalid_markers = ("insufficient_samples", "stale", "missing:", "stuck:", "implausible_values")
        quality: DataQuality = (
            "invalid" if any(i.startswith(invalid_markers) for i in issues)
            else "degraded" if issues else "valid"
        )

        load_pct = None if cur is None else max(0.0, min(1.2, (cur / self.profile.rated_current_a - 0.3) / 0.75))
        bands = {
            "vibration_zone": _vib_zone(v, L),
            "vibration_trend": _trend(slopes["vibration_rms_mm_s"], 0.2, 1.0),
            "temperature_band": _temp_band(t, L),
            "temperature_trend": _trend(slopes["bearing_temp_c"], 0.5, 3.0),
            "load_band": "unknown" if load_pct is None else
                         "light" if load_pct < 0.5 else "normal" if load_pct <= 0.85 else "heavy",
        }
        protective = (t is not None and t >= L.temp_trip) or (v is not None and v >= L.vib_trip)
        self._trip_latched = self._trip_latched or protective
        protective = self._trip_latched
        snap = EdgeSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            asset_id=self.profile.asset_id,
            window_start=buf[0].ts.isoformat() if buf else now.isoformat(),
            window_end=buf[-1].ts.isoformat() if buf else now.isoformat(),
            built_at=now.isoformat(),
            samples=len(buf),
            latest=latest,
            features={
                "vibration_slope_per_10min": _r(slopes["vibration_rms_mm_s"]),
                "temperature_slope_per_10min": _r(slopes["bearing_temp_c"]),
                "load_pct": _r(load_pct),
                "missing_ratio_vibration": _r(missing["vibration_rms_mm_s"]),
                "missing_ratio_temperature": _r(missing["bearing_temp_c"]),
            },
            bands=bands,
            data_quality=quality,
            quality_issues=tuple(issues),
            protective_limit_reached=protective,
        )
        h = hashlib.sha256(json.dumps(snap.to_dict(), sort_keys=True).encode()).hexdigest()[:16]
        return EdgeSnapshot(**{**asdict(snap), "quality_issues": snap.quality_issues, "snapshot_hash": h})


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def _vib_zone(v: float | None, L: SensorLimits) -> str:
    if v is None:
        return "unknown"
    return ("A_good" if v <= L.vib_zone_b else "B_acceptable" if v <= L.vib_zone_c
            else "C_alert" if v <= L.vib_zone_d else "D_danger")


def _temp_band(t: float | None, L: SensorLimits) -> str:
    if t is None:
        return "unknown"
    return ("normal" if t < L.temp_elevated else "elevated" if t < L.temp_high
            else "high" if t < L.temp_trip else "trip")


def _trend(slope: float | None, stable: float, fast: float) -> str:
    if slope is None:
        return "unknown"
    if slope < -stable:
        return "falling"
    return "stable" if slope < stable else "rising" if slope < fast else "rising_fast"
