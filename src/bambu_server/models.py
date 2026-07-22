"""Lab equipment status spec v1.0 models.

These models mirror the authoritative contract in
``ac-organic-lab/docs/STATUS_SPEC.md``. Bambu printers use ``other`` because
``3d_printer`` is not currently a member of the contract's closed kind enum.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1.0"

EquipmentKind = Literal[
    "solid_doser",
    "liquid_handler",
    "press",
    "fume_hood",
    "robot_arm",
    "environmental_sensor",
    "hplc",
    "plate_reader",
    "plate_sealer",
    "plate_stacker",
    "shaker",
    "camera",
    "smart_plug",
    "power_strip",
    "other",
]

EquipmentState = Literal[
    "ready",
    "busy",
    "requires_init",
    "degraded",
    "dry_run",
    "error",
    "e_stop",
    "unknown",
]


class ComponentStatus(BaseModel):
    connected: bool
    state: str
    message: str | None = None
    last_event_at: datetime | None = None


class MetricValue(BaseModel):
    value: float | int | str | bool
    unit: str | None = None
    timestamp: datetime | None = None


class ErrorInfo(BaseModel):
    code: str | None = None
    message: str
    severity: Literal["info", "warning", "error", "critical"]
    timestamp: datetime


class EquipmentStatus(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    equipment_id: str
    equipment_name: str
    equipment_kind: EquipmentKind
    equipment_version: str | None = None
    host: str | None = None
    equipment_status: EquipmentState
    message: str | None = None
    required_actions: list[str] = Field(default_factory=list)
    device_time: datetime
    uptime_seconds: float | None = None
    components: dict[str, ComponentStatus] = Field(default_factory=dict)
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    last_error: ErrorInfo | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ProbeResponse(BaseModel):
    equipment_id: str
    equipment_name: str
    protocol_version: str = PROTOCOL_VERSION


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"


class GatewayInfo(BaseModel):
    service: str
    version: str
    mode: Literal["monitoring_only"] = "monitoring_only"
    printer_count: int


class PrinterSummary(BaseModel):
    id: str
    name: str
    model: str | None = None
    status_path: str
