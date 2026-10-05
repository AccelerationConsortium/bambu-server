"""Narrow adapter around the command-capable third-party client.

Monitoring reads go through :meth:`BambuLabsBackend.read`. The control plane's
three verbs -- upload a file, start the print it names, stop the running print
-- are the *only* command methods exposed, and only the dispatch gate
(:mod:`bambu_server.dispatch`) calls them, under a claim, after the
preconditions pass. Nothing else in the library's command surface is reachable
from here.
"""

from __future__ import annotations

import logging
import math
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict

import bambulabs_api as bambu

from .config import PrinterCredentials, PrinterDefinition

logger = logging.getLogger(__name__)

#: The printer's chamber-camera service (P1 series: authenticated JPEG frames).
_CAMERA_PORT = 6000


class PrinterCommandError(RuntimeError):
    """A command could not be delivered to the printer.

    Raised only when delivery itself failed (no connection, the publish was not
    confirmed, an upload could not be verified). Delivery is not execution: a
    command that was delivered may still not have been acted on, which is the
    dispatch step's job to observe.
    """


@dataclass(frozen=True)
class UploadResult:
    remote_name: str
    byte_size: int


@dataclass(frozen=True)
class StartPrintCommand:
    """Everything the printer's ``project_file`` command needs, resolved."""

    remote_name: str
    plate_index: int
    bed_type: str
    use_ams: bool
    #: Per filament (in slicer order), the printer-side tray index.
    ams_mapping: tuple[int, ...]
    bed_leveling: bool = True
    flow_calibration: bool = False
    vibration_calibration: bool = False
    timelapse: bool = False


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

    # -- control plane (dispatch only) ---------------------------------------

    def upload_file(self, path: Path, remote_name: str) -> UploadResult: ...

    def start_print(self, command: StartPrintCommand) -> None: ...

    def stop_print(self) -> None: ...

    def current_job_file(self) -> str | None: ...

    def snapshot(self, timeout_s: float = 10.0) -> bytes: ...


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
    """``bambulabs_api.Printer`` with MQTT monitoring plus the control verbs.

    The library's camera thread is never started; a snapshot opens its own
    short-lived connection. The FTP client is used for exactly one thing --
    uploading an artifact dispatch is about to start -- and the connection is
    opened and closed around that single transfer. No command method is
    reachable from an HTTP handler except through :mod:`bambu_server.dispatch`.
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

    # -- control plane -----------------------------------------------------

    def upload_file(self, path: Path, remote_name: str) -> UploadResult:
        """Upload ``path`` to the printer's storage root as ``remote_name``.

        The library's own ``upload_file`` wraps every exception into a log line
        and returns ``None``, so a failed transfer would look like a success.
        This drives its FTPS client directly instead: connect, store, then ask
        the printer for the stored size and refuse to call the upload done
        unless it matches the local file byte for byte.
        """

        expected = path.stat().st_size
        ftp = self._printer.ftp_client
        ftps = ftp.ftps
        try:
            ftps.connect(host=ftp.server_ip, port=ftp.port)
            ftps.login(ftp.user, ftp.access_code)
            ftps.prot_p()
            with path.open("rb") as handle:
                ftps.storbinary(f"STOR {remote_name}", handle, blocksize=32768)
            stored = ftps.size(remote_name)
        except Exception as exc:  # ftplib's error hierarchy is wide and not ours
            raise PrinterCommandError(f"upload failed: {type(exc).__name__}") from exc
        finally:
            try:
                ftps.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not news
                pass
        if stored != expected:
            raise PrinterCommandError(
                f"upload not verified: printer reports {stored} bytes, expected {expected}"
            )
        return UploadResult(remote_name=remote_name, byte_size=expected)

    def start_print(self, command: StartPrintCommand) -> None:
        """Publish the ``project_file`` command for an uploaded 3mf.

        Built here rather than through the library's ``start_print``, which
        hard-codes ``bed_type`` to a textured plate: the plate is a machine
        fact the gateway has already checked, and naming the wrong one makes
        the printer heat the bed for a surface it does not have. The payload
        otherwise mirrors the library's (and Bambu Studio's) field set.
        """

        payload = {
            "print": {
                "command": "project_file",
                "param": f"Metadata/plate_{command.plate_index}.gcode",
                "file": command.remote_name,
                "url": f"ftp:///{command.remote_name}",
                "bed_type": command.bed_type,
                "bed_leveling": command.bed_leveling,
                "flow_cali": command.flow_calibration,
                "vibration_cali": command.vibration_calibration,
                "timelapse": command.timelapse,
                "layer_inspect": False,
                "use_ams": command.use_ams,
                "ams_mapping": list(command.ams_mapping),
                "skip_objects": None,
                "sequence_id": "10000000",
            }
        }
        self._publish(payload)

    def stop_print(self) -> None:
        self._publish({"print": {"command": "stop"}})

    def current_job_file(self) -> str | None:
        """The file the printer says it is printing, from the cached telemetry."""

        try:
            name = self._printer.mqtt_client.gcode_file()
        except Exception:  # noqa: BLE001 - a missing field is "not reported"
            return None
        return str(name).strip() or None

    def snapshot(self, timeout_s: float = 10.0) -> bytes:
        """One JPEG frame from the printer's chamber camera.

        Opens its own authenticated TLS connection to the camera port, reads
        until a complete frame arrives, and closes. The library's camera
        client is a perpetual reconnecting thread; a confirmation step wants
        one current picture, not a stream, so this does not use it.
        """

        host = self._printer.ip_address
        access_code = str(self._printer.access_code)
        auth = bytearray()
        auth += struct.pack("<IIII", 0x40, 0x3000, 0, 0)
        auth += b"bblp".ljust(32, b"\0")
        auth += access_code.encode("ascii").ljust(32, b"\0")

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the printer presents a self-signed cert
        deadline = time.monotonic() + timeout_s
        try:
            with socket.create_connection((host, _CAMERA_PORT), timeout=timeout_s) as raw:
                with context.wrap_socket(raw, server_hostname=host) as sock:
                    sock.settimeout(timeout_s)
                    sock.sendall(bytes(auth))
                    frame = bytearray()
                    expected: int | None = None
                    while time.monotonic() < deadline:
                        chunk = sock.recv(4096)
                        if not chunk:
                            raise PrinterCommandError("camera closed the connection (access code?)")
                        if expected is None:
                            if len(chunk) < 16:
                                raise PrinterCommandError("camera sent a malformed frame header")
                            expected = int.from_bytes(chunk[0:4], "little")
                            frame += chunk[16:]
                        else:
                            frame += chunk
                        if expected is not None and len(frame) >= expected:
                            image = bytes(frame[:expected])
                            if image[:2] != b"\xff\xd8" or image[-2:] != b"\xff\xd9":
                                raise PrinterCommandError("camera frame is not a JPEG")
                            return image
        except PrinterCommandError:
            raise
        except (OSError, ssl.SSLError) as exc:
            raise PrinterCommandError(f"camera unreachable: {type(exc).__name__}") from exc
        raise PrinterCommandError("camera did not deliver a frame in time")

    def _publish(self, payload: dict[str, object]) -> None:
        """Publish one command and insist the broker accepted it.

        Uses the library's own publish path (connection check + wait for the
        broker's acknowledgement) via its name-mangled helper; a library
        upgrade that renames it fails loudly here rather than silently
        skipping the command.
        """

        publish = getattr(
            self._printer.mqtt_client, "_PrinterMQTTClient__publish_command", None
        )
        if publish is None:
            raise PrinterCommandError("bambulabs_api publish helper not found; library changed")
        if not self._printer.mqtt_client_connected():
            raise PrinterCommandError("MQTT is not connected")
        if not publish(payload):
            raise PrinterCommandError("the MQTT broker did not confirm the command")
        logger.info("Published %s command", payload["print"]["command"])  # type: ignore[index]

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
