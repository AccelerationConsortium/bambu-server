"""Narrow monitoring adapter around the command-capable third-party client."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import bambulabs_api as bambu

from .config import PrinterCredentials, PrinterDefinition


@dataclass(frozen=True)
class PrinterReading:
    data_updated_at: datetime | None
    connected: bool
    data_ready: bool
    gcode_state: str = "UNKNOWN"
    activity: str = "UNKNOWN"
    bed_temperature_c: float | None = None
    nozzle_temperature_c: float | None = None
    chamber_temperature_c: float | None = None
    progress_percent: int | float | None = None
    remaining_time_minutes: int | float | None = None
    current_layer: int | None = None
    total_layers: int | None = None
    print_speed_percent: int | None = None
    light_state: str | None = None
    job_name: str | None = None
    firmware_version: str | None = None


class PrinterBackend(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read(self) -> PrinterReading: ...


def _number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class BambuLabsBackend:
    """MQTT-only use of ``bambulabs_api.Printer``.

    Camera and FTP clients are never started. Public command methods remain
    deliberately unreachable from the HTTP layer.
    """

    def __init__(
        self,
        definition: PrinterDefinition,
        credentials: PrinterCredentials,
    ) -> None:
        self._printer = bambu.Printer(
            credentials.host.get_secret_value(),
            credentials.access_code.get_secret_value(),
            credentials.serial.get_secret_value(),
        )
        self._lock = threading.Lock()
        self._data_updated_at: datetime | None = None
        self._printer.mqtt_client.on_message_handler = self._on_message

    def _on_message(self, *_args: object) -> None:
        with self._lock:
            self._data_updated_at = datetime.now(UTC)

    def start(self) -> None:
        self._printer.mqtt_start()

    def stop(self) -> None:
        self._printer.mqtt_stop()

    def read(self) -> PrinterReading:
        connected = bool(self._printer.mqtt_client_connected())
        ready = bool(self._printer.mqtt_client_ready())
        with self._lock:
            data_updated_at = self._data_updated_at
        if not connected or not ready:
            return PrinterReading(
                data_updated_at=data_updated_at,
                connected=connected,
                data_ready=ready,
            )

        return PrinterReading(
            data_updated_at=data_updated_at,
            connected=True,
            data_ready=True,
            gcode_state=str(self._printer.get_state()),
            activity=str(self._printer.get_current_state()),
            bed_temperature_c=_number(self._printer.get_bed_temperature()),
            nozzle_temperature_c=_number(self._printer.get_nozzle_temperature()),
            chamber_temperature_c=_number(self._printer.get_chamber_temperature()),
            progress_percent=_number(self._printer.get_percentage()),
            remaining_time_minutes=_number(self._printer.get_time()),
            current_layer=int(self._printer.current_layer_num()),
            total_layers=int(self._printer.total_layer_num()),
            print_speed_percent=int(self._printer.get_print_speed()),
            light_state=self._printer.get_light_state(),
            job_name=self._printer.get_file_name() or None,
            firmware_version=self._printer.mqtt_client.firmware_version(),
        )
