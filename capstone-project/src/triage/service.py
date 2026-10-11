# capstone-project/src/triage/service.py
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException

from triage.fakes import InMemoryHelpdesk, InMemoryLedger, InMemorySecOps   # replace with real adapters
from triage.state_builder import InvalidReport
from triage.wiring import build_engine

app = FastAPI(title="triage-engine")
engine = build_engine(InMemoryHelpdesk(), InMemorySecOps(), InMemoryLedger())


@app.post("/triage")
def triage(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return asdict(engine.handle(payload))
    except InvalidReport as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    br = engine.decider.breaker.state.value  # type: ignore[attr-defined]
    return {"breaker": br, "counters": engine.metrics.counters}
