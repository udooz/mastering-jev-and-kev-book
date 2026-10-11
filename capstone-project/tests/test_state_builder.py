# capstone-project/tests/test_state_builder.py
from datetime import datetime, timezone
from triage.state_builder import build_questions, build_state, parse_report

NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)

PAYLOAD = {
    "report_id": "R-1",
    "body": "Two payments I never made, 30,000 each, at 3am. Block my card!\n--\nSent from my iPhone",
    "customer": {"tenure_days": 400},
    "signals": {"new_device": True, "geo_mismatch": True},
    "transactions": [
        {"id": "t1", "amount_inr": 30000, "merchant": "XYZ Games", "at": "2026-09-23T09:30:00+00:00",
         "status": "posted", "disputed": True},
        {"id": "t2", "amount_inr": 30000, "merchant": "XYZ Games", "at": "2026-09-23T09:31:00+00:00",
         "status": "posted", "disputed": True},
    ],
}


def test_state_is_minimal_and_computed() -> None:
    s = build_state(parse_report(PAYLOAD), now=NOW)
    assert "Sent from my iPhone" not in s["report"]["body"]
    assert s["computed_signals"]["disputed_amount_band"] == "above_50k"   # 60k, computed in code
    assert s["computed_signals"]["txn_velocity_last_hour"] == "few"


def test_refund_target_removed_when_nothing_posted() -> None:
    p = {**PAYLOAD, "transactions": []}
    qs = build_questions(parse_report(p))
    assert "refund_pipeline" not in qs["dispatch_target"]["criteria"]
