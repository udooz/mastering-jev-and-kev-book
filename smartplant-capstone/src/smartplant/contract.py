"""SmartPlant decision contract: four bounded questions (+1 speculative) about one asset state."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Final, Literal, Mapping

CONTRACT_VERSION: Final[str] = "smartplant-contract/1.0.0"

Urgency = Literal["ROUTINE", "PRIORITY", "URGENT", "CRITICAL"]
Action = Literal["MONITOR", "INSPECT", "SCHEDULE_MAINTENANCE", "TRANSFER_LOAD", "REQUEST_SHUTDOWN"]
URGENCIES: Final[tuple[Urgency, ...]] = ("ROUTINE", "PRIORITY", "URGENT", "CRITICAL")
ACTIONS: Final[tuple[Action, ...]] = (
    "MONITOR", "INSPECT", "SCHEDULE_MAINTENANCE", "TRANSFER_LOAD", "REQUEST_SHUTDOWN",
)

SMARTPLANT_QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    "intervention_urgency": {
        "type": "choice",
        "instructions": "How soon does `asset` need a maintenance intervention, given `telemetry`, "
                        "`data_quality` and `maintenance`?",
        "criteria": {
            "ROUTINE": "Readings are in normal bands with stable trends; next planned service is fine",
            "PRIORITY": "One signal is outside its normal band or trending up, but no protective "
                        "limit is near; act within days",
            "URGENT": "Several signals are degraded or one is rising fast toward a protective limit; "
                      "act this shift",
            "CRITICAL": "A protective limit is reached or imminent, or damage is likely within the hour",
        },
    },
    "operational_action": {
        "type": "choice",
        "instructions": "Which response should operations take next for `asset`, considering "
                        "`telemetry`, `production` and `redundancy`?",
        "criteria": {
            "MONITOR": "Asset is healthy or the change is minor; keep normal monitoring",
            "INSPECT": "An operator should physically inspect the asset to confirm the condition",
            "SCHEDULE_MAINTENANCE": "Condition is confirmed or clear enough to plan a repair at "
                                    "the next suitable opportunity",
            "TRANSFER_LOAD": "Asset should be unloaded by moving production to the backup in `redundancy`",
            "REQUEST_SHUTDOWN": "Asset should be stopped in a controlled way because running on "
                                "risks damage or safety",
        },
    },
    "standard_workflow": {
        "type": "noul",
        "instructions": "Can this case be handled through the standard maintenance work-order workflow, "
                        "without escalation to a reliability specialist?",
        "criteria": {
            "true": "Signals agree, data is trustworthy, and the pattern is a familiar wear or "
                    "service issue",
            "false": "Signals conflict, data quality is questionable, or the pattern is unusual",
        },
    },
    "operational_severity": {
        "type": "score",
        "instructions": "How severe is the current condition of `asset`?",
        "criteria": [
            {"what": "No abnormal condition", "examples": ["all bands normal, trends stable"]},
            {"what": "Minor deviation with no functional impact",
             "examples": ["vibration in acceptable zone, slowly rising"]},
            {"what": "Degraded condition that will worsen without action",
             "examples": ["vibration in alert zone and rising, temperature elevated"]},
            {"what": "Serious condition; continued running risks equipment damage",
             "examples": ["vibration in danger zone or temperature high and rising fast"]},
            {"what": "Protective limit reached or failure imminent",
             "examples": ["temperature at trip band", "vibration above danger zone and climbing fast"]},
        ],
    },
}

SPECULATIVE_QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    "signals_conflict": {
        "type": "noul",
        "instructions": "Do the signals in `telemetry` point in different directions, for example "
                        "temperature rising while vibration stays normal?",
    },
}


class ContractError(ValueError):
    pass


def validate(questions: Mapping[str, Mapping[str, Any]]) -> None:
    for qid, q in questions.items():
        if not str(q.get("instructions", "")).strip():
            raise ContractError(f"{qid}: instructions required")
        crit = q.get("criteria")
        if q["type"] == "choice" and not 2 <= len(crit or {}) <= 255:
            raise ContractError(f"{qid}: Choice needs 2..255 options")
        if q["type"] == "score" and not 2 <= len(crit or []) <= 10:   # portable across Jev (2-10) and Kev
            raise ContractError(f"{qid}: Score needs 2..10 levels")
    if set(questions["operational_action"]["criteria"]) - set(ACTIONS):
        raise ContractError("operational_action has labels outside ACTIONS")


def eligible_actions(state: Mapping[str, Any]) -> list[Action]:
    """Only offer actions that are valid NOW. Rules live in code, never in criteria text."""
    allowed = set(ACTIONS)
    if state["redundancy"]["backup_coverage"] == "none":
        allowed.discard("TRANSFER_LOAD")
    return [a for a in ACTIONS if a in allowed]


def build_questions(state: Mapping[str, Any], *, speculative: bool = True) -> dict[str, dict[str, Any]]:
    qs = copy.deepcopy(SMARTPLANT_QUESTIONS)
    keep = set(eligible_actions(state))
    qs["operational_action"]["criteria"] = {
        k: v for k, v in qs["operational_action"]["criteria"].items() if k in keep
    }
    if speculative:
        qs.update(copy.deepcopy(SPECULATIVE_QUESTIONS))
    validate(qs)
    return qs


def contract_hash(questions: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest()[:12]


def to_severity_1_5(raw: float) -> int:
    """Kev/Jev return 0..4 (probability-weighted mean). Plant scale is integer 1..5."""
    if not 0.0 <= raw <= 4.0:
        raise ContractError(f"severity out of range: {raw}")
    return int(min(5, max(1, round(raw) + 1)))


validate(SMARTPLANT_QUESTIONS)
