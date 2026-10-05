from __future__ import annotations

import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bambu_server.backend import (
    AmsTrayReading,
    PrinterCommandError,
    PrinterReading,
    StartPrintCommand,
    UploadResult,
)
from bambu_server.config import Settings
from bambu_server.main import create_app

# A Bambu-shaped slicer header followed by a short toolpath. The motion spans
# X 10..110 and Y 10..60, so its footprint is 100 x 50 mm -- small enough to fit
# the test machine's 256 x 256 plate and large enough that a bed-size check has
# something real to measure.
SAMPLE_GCODE = """; HEADER_BLOCK_START
; BambuStudio 01.09.00.70
; model printing time: 1h 2m 3s; total estimated time: 1h 10m 0s
; HEADER_BLOCK_END

; CONFIG_BLOCK_START
; curr_bed_type = Textured PEI Plate
; textured_plate_temp = 55
; hot_plate_temp = 60
; chamber_temperature = 0
; filament_type = PLA
; layer_height = 0.2
; nozzle_diameter = 0.4
; nozzle_temperature = 220
; nozzle_type = hardened_steel
; printer_model = Bambu Lab X1 Carbon
; CONFIG_BLOCK_END

G90
M140 S55
M104 S220
G1 X10 Y10 Z0.2 F3000
G1 X110 Y60 E5.0
G1 X10 Y10 E7.5
M104 S0
"""

SLICE_INFO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="4200"/>
    <metadata key="weight" value="12.34"/>
    <filament id="1" type="PLA" color="#FF0000" used_g="12.08"/>
  </plate>
</config>
"""


def write_gcode(path: Path, body: str = SAMPLE_GCODE) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def write_3mf(
    path: Path,
    *,
    plate_gcode: str | None = SAMPLE_GCODE,
    project_settings: dict[str, object] | None = None,
    slice_info: str | None = SLICE_INFO_XML,
) -> Path:
    """Build a minimal Bambu-shaped 3mf container.

    ``plate_gcode=None`` produces an *unsliced* project file -- the shape a user
    gets from "save project" rather than "export plate sliced file", which no
    printer can run.
    """

    settings = {
        "printer_model": "Bambu Lab X1 Carbon",
        "nozzle_diameter": ["0.4"],
        "nozzle_temperature": ["220"],
        "curr_bed_type": "Textured PEI Plate",
        "textured_plate_temp": ["55"],
        "chamber_temperature": ["0"],
        "filament_type": ["PLA"],
        "layer_height": "0.2",
    }
    if project_settings is not None:
        settings.update(project_settings)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0"?><model unit="millimeter"><resources/></model>',
        )
        archive.writestr("Metadata/project_settings.config", json.dumps(settings))
        if slice_info is not None:
            archive.writestr("Metadata/slice_info.config", slice_info)
        if plate_gcode is not None:
            archive.writestr("Metadata/plate_1.gcode", plate_gcode)
    return path


class FakeBackend:
    """A printer that never exists. Control calls are recorded, not performed."""

    def __init__(self, reading: PrinterReading) -> None:
        self.reading = reading
        self.started = False
        self.stopped = False
        self.read_count = 0
        # Control-plane bookkeeping. Every call is recorded so a test can
        # assert exactly what would have reached a printer -- and that nothing
        # did when a gate closed.
        self.uploads: list[tuple[str, int]] = []
        self.start_commands: list[StartPrintCommand] = []
        self.stop_calls = 0
        self.snapshots = 0
        self.fail_upload = False
        self.fail_snapshot = False
        #: When True, a start command flips the reading to RUNNING, as a real
        #: printer would shortly after accepting it.
        self.start_runs = True
        self.job_file: str | None = None
        self.refresh_timestamps = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read(self) -> PrinterReading:
        self.read_count += 1
        if self.refresh_timestamps:
            self.reading = replace(self.reading, data_updated_at=datetime.now(UTC))
        return self.reading

    def upload_file(self, path: Path, remote_name: str) -> UploadResult:
        if self.fail_upload:
            raise PrinterCommandError("upload not verified: printer reports 0 bytes")
        size = path.stat().st_size
        self.uploads.append((remote_name, size))
        return UploadResult(remote_name=remote_name, byte_size=size)

    def start_print(self, command: StartPrintCommand) -> None:
        self.start_commands.append(command)
        if self.start_runs:
            self.job_file = command.remote_name
            self.reading = replace(self.reading, gcode_state="RUNNING", activity="PRINTING")

    def stop_print(self) -> None:
        self.stop_calls += 1

    def current_job_file(self) -> str | None:
        return self.job_file

    def snapshot(self, timeout_s: float = 10.0) -> bytes:
        if self.fail_snapshot:
            raise PrinterCommandError("camera unreachable: TimeoutError")
        self.snapshots += 1
        return b"\xff\xd8fake-jpeg\xff\xd9"


@pytest.fixture
def reading() -> PrinterReading:
    return PrinterReading(
        data_updated_at=datetime.now(UTC),
        connected=True,
        data_ready=True,
        gcode_state="IDLE",
        activity="IDLE",
        bed_temperature_c=25.2,
        nozzle_temperature_c=24.8,
        chamber_temperature_c=27.0,
        progress_percent=100,
        remaining_time_minutes=0,
        current_layer=120,
        total_layers=120,
        print_speed_percent=100,
        light_state="on",
        job_name="test_part.3mf",
        firmware_version="01.08.00.00",
        nozzle_type="hardened_steel",
        nozzle_diameter=0.4,
        print_type="local",
        wifi_signal="-42",
        print_error_code=0,
        skipped_objects=[3],
        ams_unit_ids=[0],
        ams_trays=[
            AmsTrayReading(
                ams_id=0,
                tray_id=1,
                tray_index=1,
                tray_type="PLA",
                tray_color="#FF0000",
                tray_weight="1000",
                tray_diameter="1.75",
                tray_temp="220",
                nozzle_temp_min=190,
                nozzle_temp_max=240,
            )
        ],
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    prefix = "BAMBU_TEST_01"
    monkeypatch.setenv(f"{prefix}_HOST", "printer.invalid")
    monkeypatch.setenv(f"{prefix}_ACCESS_CODE", "secret-access-code")
    monkeypatch.setenv(f"{prefix}_SERIAL", "secret-serial")
    return Settings.model_validate(
        {
            "poll_interval_seconds": 60,
            "stale_after_seconds": 120,
            # Submissions land in a per-test directory: the intake writes real
            # files and must never touch the repository or a shared path.
            "submissions": {"directory": str(tmp_path / "submissions")},
            "printers": [
                {
                    "id": "bambu_test_01",
                    "name": "Bambu Test 01",
                    "model": "X1 Carbon",
                    "env_prefix": prefix,
                    "profile": {
                        "enclosure": "enclosed",
                        "nozzle_type": "hardened_steel",
                        "nozzle_diameter_mm": 0.4,
                        "bed_size_mm": [256, 256],
                        "limits": {
                            "nozzle_temperature_c": [0, 300],
                            "bed_temperature_c": [0, 110],
                        },
                        "ams": {"filament_forbidden": ["ABS"]},
                    },
                }
            ],
        }
    )


@pytest.fixture
def backend(reading: PrinterReading) -> FakeBackend:
    return FakeBackend(reading)


@pytest.fixture
def client(settings: Settings, backend: FakeBackend) -> TestClient:
    app = create_app(settings=settings, backend_factory=lambda _definition, _creds: backend)
    with TestClient(app) as test_client:
        yield test_client
