"""Narrow monitoring adapter around the command-capable third-party client."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypedDict

import bambulabs_api as bambu

from .config import PrinterCredentials, PrinterDefinition


@dataclass(frozen=True)
class AmsTrayReading:
    """A single loaded AMS filament tray. Field types mirror the third-party
    ``FilamentTray`` payload, which carries some values as strings."""

    ams_id: int | None = None
    tray_id: int | None = None
    tray_index: int | None = None
    tray_type: str | None = None
    tray_color: str | None = None
    tray_weight: str | None = None
    tray_diameter: str | None = None
    tray_temp: str | None = None
    # The filament's own nozzle-temperature window, as reported by the spool
    # tag. Carried as integers because that is how the payload types them, and
    # because the submission validator compares a model's configured nozzle
    # temperature against this window.
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    remaining_percent: int | None = None


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
    nozzle_type: str | None = None
    nozzle_diameter: float | None = None
    print_type: str | None = None
    wifi_signal: str | None = None
    print_error_code: int | None = None
    skipped_objects: list[int] | None = None
    ams_trays: list[AmsTrayReading] | None = None
    ams_unit_ids: list[int] | None = None


class AdvancedReading(TypedDict):
    """The optional enrichment fields, typed so ``**`` spreads against
    :class:`PrinterReading`'s constructor without a static mismatch."""

    nozzle_type: str | None
    nozzle_diameter: float | None
    print_type: str | None
    wifi_signal: str | None
    print_error_code: int | None
    skipped_objects: list[int] | None
    ams_trays: list[AmsTrayReading] | None
    ams_unit_ids: list[int] | None


class PrinterBackend(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read(self) -> PrinterReading: ...


def _optional_int(value: object) -> int | None:
    """Coerce a tray field to ``int`` or drop it.

    Tray payload fields arrive as ints or numeric strings depending on firmware,
    and an unparsable value is reported as absent rather than as a temperature
    the printer never stated.
    """

    number = _number(value)
    if number is None or not math.isfinite(number):
        return None
    integer = int(number)
    return integer if integer == number else None


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

        advanced = self._advanced_reading()
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
            **advanced,
        )

    def _advanced_reading(self) -> AdvancedReading:
        """Best-effort advanced telemetry (nozzle, print metadata, wifi, AMS).

        These are optional enrichment: a failure in any one getter must not
        poison the whole reading, so each is isolated. Sentinel/default values
        (0.0 nozzle diameter, empty strings, ``stainless_steel``) are collapsed
        to ``None`` rather than reported as facts we did not observe.
        """
        values: AdvancedReading = {
            "nozzle_type": None,
            "nozzle_diameter": None,
            "print_type": None,
            "wifi_signal": None,
            "print_error_code": None,
            "skipped_objects": None,
            "ams_trays": None,
            "ams_unit_ids": None,
        }

        try:
            nozzle_type = self._printer.nozzle_type()
            values["nozzle_type"] = str(nozzle_type) if nozzle_type else None
        except Exception:
            pass
        try:
            diameter = self._printer.nozzle_diameter()
            values["nozzle_diameter"] = float(diameter) if diameter else None
        except Exception:
            pass
        try:
            print_type = self._printer.print_type()
            values["print_type"] = print_type.strip() or None
        except Exception:
            pass
        try:
            wifi_signal = self._printer.wifi_signal()
            values["wifi_signal"] = wifi_signal.strip() or None
        except Exception:
            pass
        try:
            error_code = self._printer.print_error_code()
            values["print_error_code"] = int(error_code) if error_code is not None else None
        except Exception:
            pass
        try:
            skipped = self._printer.get_skipped_objects()
            values["skipped_objects"] = [int(obj) for obj in (skipped or [])] or None
        except Exception:
            pass

        values["ams_unit_ids"], values["ams_trays"] = self._read_ams_inventory()
        return values

    def _read_ams_inventory(self) -> tuple[list[int] | None, list[AmsTrayReading] | None]:
        """Decode only allowlisted fields from the MQTT client's local cache.

        The 2.6.6 hub conversion requires ``ams_exist_bits`` and truthy ``n``;
        these optional fields are absent on some printers. ``n`` is not a slot
        address. Explicit unit/tray IDs identify inventory, including HT units.
        No getter that requests a refresh is called here, and no raw payload
        or spool identifiers escape the adapter.
        """
        try:
            payload = self._printer.mqtt_client.dump().get("print", {}).get("ams")
            if not isinstance(payload, dict) or not isinstance(payload.get("ams"), list):
                return None, None
            unit_ids: list[int] = []
            trays: list[AmsTrayReading] = []
            for unit in payload["ams"]:
                ams_id = _optional_int(unit.get("id"))
                if ams_id is None or ams_id < 0 or ams_id in unit_ids:
                    return None, None
                unit_ids.append(ams_id)
                unit_trays = unit.get("tray")
                if not isinstance(unit_trays, list):
                    return None, None
                seen: set[int] = set()
                for tray in unit_trays:
                    tray_id = _optional_int(tray.get("id"))
                    if tray_id is None or tray_id < 0 or tray_id in seen:
                        return None, None
                    seen.add(tray_id)
                    material = _tray_text(tray.get("tray_type"))
                    # An id-only slot is empty; optional calibration/spool-tag
                    # fields are not evidence that filament is loaded.
                    if material is None:
                        continue
                    remaining = _optional_int(tray.get("remain"))
                    trays.append(
                        AmsTrayReading(
                            ams_id=ams_id,
                            tray_id=tray_id,
                            tray_type=material,
                            tray_color=_tray_text(tray.get("tray_color")),
                            tray_weight=_tray_text(tray.get("tray_weight")),
                            tray_diameter=_tray_text(tray.get("tray_diameter")),
                            tray_temp=_tray_text(tray.get("tray_temp")),
                            nozzle_temp_min=_optional_int(tray.get("nozzle_temp_min")),
                            nozzle_temp_max=_optional_int(tray.get("nozzle_temp_max")),
                            remaining_percent=(
                                remaining if remaining is not None and 0 <= remaining <= 100
                                else None
                            ),
                        )
                    )
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None, None
        return unit_ids, trays


def _tray_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None
