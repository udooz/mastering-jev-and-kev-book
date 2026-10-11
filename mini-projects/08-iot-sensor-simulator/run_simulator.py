"""Mini-project 8: simulate motor telemetry with faults and emit versioned edge snapshots.

uv run python mini-projects/08-iot-sensor-simulator/run_simulator.py --fault gradual_degradation
uv run --group iot python mini-projects/08-iot-sensor-simulator/run_simulator.py --mqtt localhost
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from smartplant.edge_state_builder import EdgeStateBuilder
from smartplant.fault_injector import Fault, FaultMode
from smartplant.sensor_simulator import AssetProfile, SensorSimulator


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="M-104")
    ap.add_argument("--steps", type=int, default=240)
    ap.add_argument("--snapshot-every", type=int, default=30, help="steps between snapshots (10 s/step)")
    ap.add_argument("--fault", choices=[m.value for m in FaultMode], action="append", default=[])
    ap.add_argument("--fault-start", type=int, default=60)
    ap.add_argument("--magnitude", type=float, default=1.0)
    ap.add_argument("--channel", default="bearing_temp_c")
    ap.add_argument("--out", type=Path, default=Path("snapshots.jsonl"))
    ap.add_argument("--mqtt", default=None, help="broker host: publish readings to plant/<asset>/telemetry")
    args = ap.parse_args()

    profile = AssetProfile(asset_id=args.asset)
    faults = [Fault(FaultMode(m), args.fault_start, magnitude=args.magnitude, channel=args.channel)
              for m in args.fault]
    sim, builder = SensorSimulator(profile, faults=faults), EdgeStateBuilder(profile)
    mqtt = None
    if args.mqtt:
        import paho.mqtt.client as paho          # optional: uv sync --group iot
        mqtt = paho.Client(paho.CallbackAPIVersion.VERSION2)
        mqtt.connect(args.mqtt, 1883)
        mqtt.loop_start()

    with args.out.open("w", encoding="utf-8") as f:
        for r in sim.stream(args.steps):
            builder.add(r)
            if mqtt:
                mqtt.publish(f"plant/{r.asset_id}/telemetry", r.to_json(), qos=1)
            if r.seq and r.seq % args.snapshot_every == 0:
                s = builder.snapshot(r.ts + timedelta(seconds=1))
                f.write(json.dumps(s.to_dict()) + "\n")
                print(f"t={r.seq:4d} vib={s.latest['vibration_rms_mm_s']!s:>6} temp={s.latest['bearing_temp_c']!s:>6} "
                      f"{s.bands['vibration_zone']:13} {s.bands['vibration_trend']:11} "
                      f"{s.bands['temperature_band']:8} {s.data_quality:8} {','.join(s.quality_issues) or '-'}")
    if mqtt:
        mqtt.loop_stop()
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
