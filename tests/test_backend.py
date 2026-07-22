from __future__ import annotations

from datetime import UTC, datetime

from bambu_server import backend as backend_module
from bambu_server.backend import BambuLabsBackend
from bambu_server.config import PrinterCredentials, PrinterDefinition


class FakeMqttClient:
    def __init__(self) -> None:
        self.on_message_handler = None

    def firmware_version(self) -> str:
        return "01.08.00.00"


class FakePrinter:
    def __init__(self, host: str, access_code: str, serial: str) -> None:
        self.constructor_values = (host, access_code, serial)
        self.mqtt_client = FakeMqttClient()
        self.mqtt_started = False
        self.mqtt_stopped = False

    def mqtt_start(self) -> None:
        self.mqtt_started = True

    def mqtt_stop(self) -> None:
        self.mqtt_stopped = True

    def mqtt_client_connected(self) -> bool:
        return True

    def mqtt_client_ready(self) -> bool:
        return True

    def get_state(self) -> str:
        return "RUNNING"

    def get_current_state(self) -> str:
        return "PRINTING"

    def get_bed_temperature(self) -> float:
        return 60.0

    def get_nozzle_temperature(self) -> float:
        return 220.0

    def get_chamber_temperature(self) -> float:
        return 35.0

    def get_percentage(self) -> int:
        return 42

    def get_time(self) -> int:
        return 18

    def current_layer_num(self) -> int:
        return 21

    def total_layer_num(self) -> int:
        return 50

    def get_print_speed(self) -> int:
        return 100

    def get_light_state(self) -> str:
        return "on"

    def get_file_name(self) -> str:
        return "part.3mf"


def test_backend_starts_only_mqtt_and_builds_reading(monkeypatch) -> None:
    fake = FakePrinter("printer.invalid", "access-secret", "serial-secret")
    monkeypatch.setattr(backend_module.bambu, "Printer", lambda *_args: fake)
    backend = BambuLabsBackend(
        PrinterDefinition(id="bambu_one", name="One", env_prefix="BAMBU_ONE"),
        PrinterCredentials(
            host="printer.invalid",
            access_code="access-secret",
            serial="serial-secret",
        ),
    )

    backend.start()
    assert fake.mqtt_started is True
    assert fake.mqtt_client.on_message_handler is not None
    fake.mqtt_client.on_message_handler(None)
    reading = backend.read()
    backend.stop()

    assert fake.mqtt_stopped is True
    assert reading.data_updated_at is not None
    assert datetime.now(UTC) >= reading.data_updated_at
    assert reading.gcode_state == "RUNNING"
    assert reading.progress_percent == 42
    assert reading.remaining_time_minutes == 18
