"""Append-only, hash-chained audit trail: snapshot -> decision -> rules -> approval -> action -> outcome.

Each record carries the SHA-256 of the previous one, so editing or deleting any line breaks
`verify_chain`. Ship the file (or each record) to WORM storage for real immutability.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

EventType = Literal["snapshot", "decision", "approval", "execution", "verification", "outcome", "fallback"]
GENESIS = "0" * 64


def _digest(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


@dataclass(slots=True)
class AuditLog:
    path: Path
    _last: str = GENESIS
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").splitlines()
            if lines:
                self._last = json.loads(lines[-1])["hash"]

    def append(self, event: EventType, correlation_id: str, payload: dict[str, Any]) -> str:
        with self._lock:
            body = {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
                    "correlation_id": correlation_id, "payload": payload, "prev": self._last}
            h = _digest(self._last, body)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**body, "hash": h}, default=str) + "\n")
            self._last = h
            return h

    def events(self, correlation_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l]
        return [r for r in rows if correlation_id is None or r["correlation_id"] == correlation_id]


def verify_chain(path: Path) -> bool:
    prev = GENESIS
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        h = rec.pop("hash")
        if rec["prev"] != prev or _digest(prev, rec) != h:
            return False
        prev = h
    return True


REQUIRED_EVENTS = ("snapshot", "decision", "execution")


def audit_complete(log: AuditLog, correlation_id: str) -> bool:
    """Can this decision be reconstructed? Input snapshot, decision and execution must all be present."""
    seen = {e["event"] for e in log.events(correlation_id)}
    return all(ev in seen for ev in REQUIRED_EVENTS)
