from __future__ import annotations

from datetime import timedelta

from smartplant.edge_state_builder import EdgeStateBuilder
from smartplant.fault_injector import Fault, FaultMode
from smartplant.sensor_simulator import SensorSimulator


def snap(faults=(), steps=120, extra_s=5):
    sim, b = SensorSimulator(faults=list(faults)), EdgeStateBuilder()
    rs = list(sim.stream(steps))
    for r in rs:
        b.add(r)
    return b.snapshot(rs[-1].ts + timedelta(seconds=extra_s))


def test_simulator_is_deterministic() -> None:
    a = [r.to_json() for r in SensorSimulator(seed=3).stream(50)]
    b = [r.to_json() for r in SensorSimulator(seed=3).stream(50)]
    assert a == b


def test_healthy_motor_is_valid_and_normal() -> None:
    s = snap()
    assert s.data_quality == "valid" and s.bands["vibration_zone"] == "A_good"
    assert s.bands["temperature_trend"] == "stable" and not s.protective_limit_reached


def test_gradual_degradation_shows_rising_vibration() -> None:
    s = snap([Fault(FaultMode.GRADUAL_DEGRADATION, 20, magnitude=1.0)])
    assert s.bands["vibration_zone"] == "D_danger" and s.bands["vibration_trend"] == "rising_fast"


def test_thermal_spike_is_signal_not_noise() -> None:
    s = snap([Fault(FaultMode.THERMAL_SPIKE, 100, magnitude=1.15)])
    assert s.bands["temperature_band"] == "high" and s.data_quality == "valid"


def test_bad_sensors_are_caught() -> None:
    assert snap([Fault(FaultMode.SENSOR_DROPOUT, 40)]).data_quality == "invalid"
    assert snap([Fault(FaultMode.STUCK_SENSOR, 40, channel="vibration_rms_mm_s")]).data_quality == "invalid"
    assert "noisy:bearing_temp_c" in snap([Fault(FaultMode.SENSOR_NOISE, 40)]).quality_issues
    assert "stale" in snap(extra_s=300).quality_issues


def test_protective_limit_mirrors_plc_trip() -> None:
    s = snap([Fault(FaultMode.THERMAL_SPIKE, 60, magnitude=1.5)])
    assert s.protective_limit_reached and s.bands["temperature_band"] == "trip"


def test_snapshot_hash_is_stable() -> None:
    assert snap().snapshot_hash == snap().snapshot_hash


def test_trip_mirror_latches_until_operator_reset() -> None:
    sim = SensorSimulator(faults=[Fault(FaultMode.THERMAL_SPIKE, 20, duration_steps=15, magnitude=1.5)])
    b = EdgeStateBuilder()
    rs = list(sim.stream(80))                  # spike is over by step 35; temperature recovers
    for r in rs:
        b.add(r)
        if r.seq == 30:
            assert b.snapshot(r.ts).protective_limit_reached
    assert b.snapshot(rs[-1].ts).protective_limit_reached        # still latched
    b.reset_trip()
    assert not b.snapshot(rs[-1].ts).protective_limit_reached
