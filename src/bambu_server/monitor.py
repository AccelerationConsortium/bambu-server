"""Background printer monitoring and STATUS_SPEC envelope assembly."""

from __future__ import annotations

import asyncio
import logging
import socket
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic

from . import __version__
from .backend import PrinterBackend, PrinterReading
from .config import PrinterDefinition
from .models import (
    PROTOCOL_VERSION,
    ComponentStatus,
    EquipmentStatus,
    ErrorInfo,
    MetricValue,
)

logger = logging.getLogger(__name__)

_READY_STATES = {"IDLE", "FINISH"}
_BUSY_STATES = {"PREPARE", "RUNNING", "PAUSE"}


class PrinterMonitor:
    def __init__(
        self,
        definition: PrinterDefinition,
        backend: PrinterBackend,
        *,
        poll_interval_seconds: float,
        stale_after_seconds: float,
    ) -> None:
        self.definition = definition
        self._backend = backend
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._started_at = monotonic()
        self._reading: PrinterReading | None = None
        self._monitor_error_type: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(self._backend.start)
        await self._collect_once()
        self._task = asyncio.create_task(
            self._run(), name=f"bambu-monitor-{self.definition.id}"
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await asyncio.to_thread(self._backend.stop)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_seconds)
            await self._collect_once()

    async def _collect_once(self) -> None:
        try:
            self._reading = await asyncio.to_thread(self._backend.read)
            self._monitor_error_type = None
        except Exception as exc:
            self._monitor_error_type = type(exc).__name__
            logger.exception("Printer monitor failed for %s", self.definition.id)

    def status(self) -> EquipmentStatus:
        now = datetime.now(UTC)
        reading = self._reading
        state = "unknown"
        message = "Waiting for the first printer observation"
        last_error = None

        if self._monitor_error_type is not None:
            message = f"Printer monitoring failed ({self._monitor_error_type})"
        elif reading is not None and not reading.connected:
            message = "Printer MQTT endpoint is unreachable"
        elif reading is not None and not reading.data_ready:
            message = "Waiting for the printer's initial MQTT state"
        elif reading is not None and self._is_stale(reading, now):
            message = "Printer MQTT state is stale"
        elif reading is not None:
            state, message, last_error = self._map_state(reading, now)

        components: dict[str, ComponentStatus] = {
            "mqtt": ComponentStatus(
                connected=bool(reading and reading.connected),
                state=(
                    "ready"
                    if reading and reading.connected and reading.data_ready
                    else "connected"
                    if reading and reading.connected
                    else "disconnected"
                ),
                last_event_at=reading.data_updated_at if reading else None,
            )
        }
        metrics = self._metrics(reading) if reading and reading.data_ready else {}
        details: dict[str, object] = {
            "device_type": "3d_printer",
            "model": self.definition.model,
            "monitoring_only": True,
            "data_updated_at": reading.data_updated_at if reading else None,
        }
        if reading and reading.data_ready:
            components["print_job"] = ComponentStatus(
                connected=True,
                state=reading.gcode_state.lower(),
                message=reading.activity.lower().replace("_", " "),
                last_event_at=reading.data_updated_at,
            )
            details.update(
                {
                    "activity": reading.activity.lower(),
                    "firmware_version": reading.firmware_version,
                    "job_name": reading.job_name,
                    "light_state": reading.light_state,
                }
            )

        return EquipmentStatus(
            protocol_version=PROTOCOL_VERSION,
            equipment_id=self.definition.id,
            equipment_name=self.definition.name,
            equipment_kind="other",
            equipment_version=__version__,
            host=socket.gethostname(),
            equipment_status=state,
            activity=("idle" if state == "ready" else "unknown"),
            activity_since=now if state == "ready" else None,
            message=message,
            device_time=now,
            uptime_seconds=monotonic() - self._started_at,
            components=components,
            metrics=metrics,
            last_error=last_error,
            allowed_actions=[],
            details=details,
        )

    def _is_stale(self, reading: PrinterReading, now: datetime) -> bool:
        if reading.data_updated_at is None:
            return True
        return (now - reading.data_updated_at).total_seconds() > self._stale_after_seconds

    @staticmethod
    def _map_state(
        reading: PrinterReading, now: datetime
    ) -> tuple[str, str, ErrorInfo | None]:
        if reading.gcode_state in _READY_STATES:
            return "ready", "Printer is idle", None
        if reading.gcode_state in _BUSY_STATES:
            return "busy", f"Print job is {reading.gcode_state.lower()}", None
        if reading.gcode_state == "FAILED":
            return (
                "error",
                "Printer reported a failed print job",
                ErrorInfo(
                    code="print_failed",
                    message="Printer reported a failed print job",
                    severity="error",
                    timestamp=reading.data_updated_at or now,
                ),
            )
        return "unknown", "Printer state is unknown", None

    @staticmethod
    def _metrics(reading: PrinterReading) -> dict[str, MetricValue]:
        timestamp = reading.data_updated_at
        values: dict[str, tuple[int | float | str | bool | None, str | None]] = {
            "bed_temperature": (reading.bed_temperature_c, "C"),
            "nozzle_temperature": (reading.nozzle_temperature_c, "C"),
            "chamber_temperature": (reading.chamber_temperature_c, "C"),
            "print_progress": (reading.progress_percent, "%"),
            "remaining_time": (reading.remaining_time_minutes, "min"),
            "current_layer": (reading.current_layer, "layer"),
            "total_layers": (reading.total_layers, "layer"),
            "print_speed": (reading.print_speed_percent, "%"),
        }
        return {
            key: MetricValue(value=value, unit=unit, timestamp=timestamp)
            for key, (value, unit) in values.items()
            if value is not None
        }
