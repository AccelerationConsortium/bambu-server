from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from bambu_server.backend import PrinterReading
from bambu_server.config import Settings
from bambu_server.main import create_app


class FakeBackend:
    def __init__(self, reading: PrinterReading) -> None:
        self.reading = reading
        self.started = False
        self.stopped = False
        self.read_count = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read(self) -> PrinterReading:
        self.read_count += 1
        return self.reading


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
    )


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    prefix = "BAMBU_TEST_01"
    monkeypatch.setenv(f"{prefix}_HOST", "printer.invalid")
    monkeypatch.setenv(f"{prefix}_ACCESS_CODE", "secret-access-code")
    monkeypatch.setenv(f"{prefix}_SERIAL", "secret-serial")
    return Settings.model_validate(
        {
            "poll_interval_seconds": 60,
            "stale_after_seconds": 120,
            "printers": [
                {
                    "id": "bambu_test_01",
                    "name": "Bambu Test 01",
                    "model": "X1 Carbon",
                    "env_prefix": prefix,
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
