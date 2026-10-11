# capstone-project/src/triage/fakes.py
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class InMemoryHelpdesk:
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def upsert_ticket(self, key: str, queue: str, priority: str, body: Mapping[str, Any]) -> str:
        tid = self.by_key.get(key) or f"T{next(self._ids):05d}"      # idempotent on key
        self.by_key[key] = tid
        self.tickets[tid] = {"queue": queue, "priority": priority, "body": dict(body)}
        return tid

    def get_ticket(self, ticket_id: str) -> Mapping[str, Any] | None:
        return self.tickets.get(ticket_id)


@dataclass(slots=True)
class InMemorySecOps:
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def open_case(self, key: str, priority: str, evidence: Mapping[str, Any]) -> str:
        cid = self.by_key.get(key) or f"SEC-{next(self._ids):04d}"
        self.by_key[key] = cid
        self.cases[cid] = {"status": "open", "priority": priority, "evidence": dict(evidence)}
        return cid

    def get_case(self, case_id: str) -> Mapping[str, Any] | None:
        return self.cases.get(case_id)


@dataclass(slots=True)
class InMemoryLedger:
    refunds: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    drop_writes: bool = False          # simulate a lost write to exercise verification

    def create_refund_request(self, key: str, needs_approval: bool, evidence: Mapping[str, Any]) -> str:
        rid = self.by_key.get(key) or f"RF-{next(self._ids):04d}"
        self.by_key[key] = rid
        if not self.drop_writes:
            self.refunds[rid] = {"status": "pending_approval" if needs_approval else "queued",
                                 "evidence": dict(evidence)}
        return rid

    def get_refund_request(self, refund_id: str) -> Mapping[str, Any] | None:
        return self.refunds.get(refund_id)
