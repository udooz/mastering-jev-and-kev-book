"""Operational context: MES (production), CMMS (maintenance), redundancy -> decision state.

The enricher computes every comparison in code (does the backup cover the load? how close is
the deadline?) and hands Kev named buckets. Kev never compares 78 % with 91 %.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal, Mapping, Protocol

from smartplant.edge_state_builder import EdgeSnapshot

DECISION_STATE_VERSION: Final[str] = "smartplant-state/1.0.0"
Priority = Literal["LOW", "NORMAL", "HIGH", "CRITICAL"]


@dataclass(frozen=True, slots=True)
class ProductionOrder:
    order_id: str
    priority: Priority
    hours_to_deadline: float
    required_load_pct: float          # load the line needs from this asset (0..1)


@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_id: str
    criticality: Literal["A", "B", "C"]           # A = line stops without it
    days_since_bearing_replacement: int
    open_work_orders: int
    spare_bearing_in_stock: bool
    last_inspection_finding: str = "none"


@dataclass(frozen=True, slots=True)
class BackupAsset:
    asset_id: str | None
    available: bool
    approved_for_process: bool
    capacity_pct: float               # fraction of the primary's load it can carry (0..1)


class MesService(Protocol):
    def current_order(self, asset_id: str) -> ProductionOrder | None: ...


class CmmsReader(Protocol):
    def asset_record(self, asset_id: str) -> AssetRecord: ...


class RedundancyService(Protocol):
    def backup_for(self, asset_id: str) -> BackupAsset: ...


@dataclass(slots=True)
class MockPlantData:
    """One object implementing all three read services, seeded per scenario."""
    orders: dict[str, ProductionOrder] = field(default_factory=dict)
    assets: dict[str, AssetRecord] = field(default_factory=dict)
    backups: dict[str, BackupAsset] = field(default_factory=dict)

    def current_order(self, asset_id: str) -> ProductionOrder | None:
        return self.orders.get(asset_id)

    def asset_record(self, asset_id: str) -> AssetRecord:
        try:
            return self.assets[asset_id]
        except KeyError as e:
            raise LookupError(f"asset {asset_id} not registered in CMMS") from e

    def backup_for(self, asset_id: str) -> BackupAsset:
        return self.backups.get(asset_id, BackupAsset(None, False, False, 0.0))


def backup_coverage(backup: BackupAsset, required_load_pct: float) -> str:
    if not (backup.available and backup.approved_for_process and backup.capacity_pct > 0):
        return "none"
    return "full" if backup.capacity_pct >= required_load_pct else "partial"


def _bearing_age_band(days: int) -> str:
    return "new" if days < 90 else "mid_life" if days < 365 else "aged"


def _deadline_band(hours: float | None) -> str:
    if hours is None:
        return "no_active_order"
    return "under_8h" if hours < 8 else "8_to_48h" if hours <= 48 else "over_48h"


def enrich(snapshot: EdgeSnapshot, mes: MesService, cmms: CmmsReader,
           redundancy: RedundancyService) -> dict[str, Any]:
    order = mes.current_order(snapshot.asset_id)
    record = cmms.asset_record(snapshot.asset_id)
    backup = redundancy.backup_for(snapshot.asset_id)
    required = order.required_load_pct if order else (snapshot.features.get("load_pct") or 0.0)
    return {
        "state_version": DECISION_STATE_VERSION,
        "snapshot_hash": snapshot.snapshot_hash,
        "asset": {"id": snapshot.asset_id, "criticality": record.criticality},
        "telemetry": {
            **snapshot.bands,
            "latest_vibration_mm_s": snapshot.latest.get("vibration_rms_mm_s"),
            "latest_bearing_temp_c": snapshot.latest.get("bearing_temp_c"),
            "window_minutes": 20,
        },
        "data_quality": {"status": snapshot.data_quality, "issues": list(snapshot.quality_issues)},
        "protective_limit_reached": snapshot.protective_limit_reached,
        "maintenance": {
            "bearing_age": _bearing_age_band(record.days_since_bearing_replacement),
            "open_work_orders": record.open_work_orders > 0,
            "spare_bearing_in_stock": record.spare_bearing_in_stock,
            "last_inspection_finding": record.last_inspection_finding,
        },
        "production": {
            "order_priority": order.priority if order else "NONE",
            "deadline": _deadline_band(order.hours_to_deadline if order else None),
        },
        "redundancy": {
            "backup_asset": backup.asset_id,
            "backup_coverage": backup_coverage(backup, required),
        },
    }


def summarize(state: Mapping[str, Any]) -> str:
    """One line for operator screens and logs."""
    t = state["telemetry"]
    return (f"{state['asset']['id']}: vib {t['vibration_zone']}/{t['vibration_trend']}, "
            f"temp {t['temperature_band']}/{t['temperature_trend']}, load {t['load_band']}, "
            f"data {state['data_quality']['status']}, order {state['production']['order_priority']}, "
            f"backup {state['redundancy']['backup_coverage']}")
