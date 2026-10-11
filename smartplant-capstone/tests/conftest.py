from __future__ import annotations

from typing import Any

import pytest

from smartplant.scenario_runner import build_state, load_scenarios


@pytest.fixture(scope="session")
def scenarios() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in load_scenarios()}


@pytest.fixture()
def state_for(scenarios):
    return lambda sid: build_state(scenarios[sid])
