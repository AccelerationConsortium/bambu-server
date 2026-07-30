"""STATUS_SPEC v1.2 models for the Bambu printer gateway.

Wire-contract types are imported from the shared ``sdl-lab-contract`` package
and re-exported. Device-specific models (``GatewayInfo``, ``PrinterSummary``)
remain local.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from sdl_lab_contract import (
    ComponentStatus,
    EquipmentKind,
    EquipmentState,
    EquipmentStatus,
    ErrorInfo,
    ErrorSeverity,
    HealthResponse,
    MetricValue,
    ProbeResponse,
)

PROTOCOL_VERSION = "1.2"


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


__all__ = [
    # Re-exported from sdl_lab_contract
    "ComponentStatus",
    "EquipmentKind",
    "EquipmentState",
    "EquipmentStatus",
    "ErrorInfo",
    "ErrorSeverity",
    "HealthResponse",
    "MetricValue",
    "ProbeResponse",
    # Local
    "PROTOCOL_VERSION",
    "GatewayInfo",
    "PrinterSummary",
]
