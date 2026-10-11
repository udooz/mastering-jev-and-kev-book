"""Mock CMMS and control-request services. Nothing here touches a real machine."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

WOStatus = Literal["open", "in_progress", "completed", "cancelled"]


class CmmsUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class WorkOrder:
    wo_id: str
    idempotency_key: str
    asset_id: str
    kind: str                 # INSPECT | SCHEDULE_MAINTENANCE | TRANSFER_LOAD | REQUEST_SHUTDOWN
    priority: str             # ROUTINE | PRIORITY | URGENT | CRITICAL
    description: str
    evidence: dict[str, Any]
    status: WOStatus = "open"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finding: str | None = None


@dataclass(slots=True)
class InMemoryCmms:
    work_orders: dict[str, WorkOrder] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    fail_next: int = 0                       # simulate outages
    drop_next_write: bool = False            # simulate an acknowledged-but-lost write
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1001))

    def create_work_order(self, idempotency_key: str, asset_id: str, kind: str, priority: str,
                          description: str, evidence: dict[str, Any]) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise CmmsUnavailable("CMMS returned 503")
        if idempotency_key in self.by_key:                 # retries never create duplicates
            return self.by_key[idempotency_key]
        wo_id = f"WO-{next(self._ids)}"
        self.by_key[idempotency_key] = wo_id
        if self.drop_next_write:
            self.drop_next_write = False
            return wo_id                                   # acknowledged, never stored
        self.work_orders[wo_id] = WorkOrder(wo_id, idempotency_key, asset_id, kind, priority,
                                            description, evidence)
        return wo_id

    def get_work_order(self, wo_id: str) -> WorkOrder | None:
        return self.work_orders.get(wo_id)

    def escalate_priority(self, wo_id: str, priority: str) -> None:
        self.work_orders[wo_id].priority = priority

    def complete(self, wo_id: str, finding: str) -> None:
        wo = self.work_orders[wo_id]
        wo.status, wo.finding = "completed", finding


@dataclass(slots=True)
class MockControlRequests:
    """Load-transfer and shutdown REQUESTS for the control-room workflow. It sends no commands."""
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    def submit(self, kind: str, asset_id: str, approved_by: str, work_order: str) -> str:
        for rid, req in self.requests.items():          # idempotent per work order
            if req["work_order"] == work_order and req["kind"] == kind:
                return rid
        rid = f"CR-{next(self._ids):04d}"
        self.requests[rid] = {"kind": kind, "asset_id": asset_id, "approved_by": approved_by,
                              "work_order": work_order, "status": "pending_control_room"}
        return rid

    def get(self, request_id: str) -> dict[str, Any] | None:
        return self.requests.get(request_id)
