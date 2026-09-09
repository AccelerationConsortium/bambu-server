from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bambu_server import backend as backend_module
from bambu_server.backend import BambuLabsBackend
from bambu_server.config import PrinterCredentials, PrinterDefinition


class FakeMqttClient:
    def __init__(self) -> None:
        self.on_message_handler = None

    def firmware_version(self) -> str:
        return "01.08.00.00"

    def dump(self) -> dict:
        return {"print": {"ams": {"ams": [{"id": "0", "tray": [{
            "id": "1", "n": 7, "tray_type": "PLA", "tray_color": "#FF0000",
            "nozzle_temp_min": 190, "nozzle_temp_max": "240",
        }]}]}}}


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

    def nozzle_type(self) -> str:
        return "hardened_steel"

    def nozzle_diameter(self) -> float:
        return 0.4

    def print_type(self) -> str:
        return "local"

    def wifi_signal(self) -> str:
        return "-42"

    def print_error_code(self) -> int:
        return 0

    def get_skipped_objects(self) -> list[int]:
        return [3]

    def ams_hub(self):
        raise AssertionError("The incompatible library hub getter must not be used")


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
    assert reading.nozzle_type == "hardened_steel"
    assert reading.nozzle_diameter == 0.4
    assert reading.print_type == "local"
    assert reading.wifi_signal == "-42"
    assert reading.print_error_code == 0
    assert reading.skipped_objects == [3]
    assert reading.ams_trays is not None
    assert reading.ams_trays[0].tray_type == "PLA"
    assert reading.ams_trays[0].tray_id == 1
    assert reading.ams_trays[0].tray_index is None
    assert reading.ams_unit_ids == [0]
    # The spool's own nozzle window, which the submission validator checks a
    # model's configured temperature against.
    assert reading.ams_trays[0].nozzle_temp_min == 190
    assert reading.ams_trays[0].nozzle_temp_max == 240


def _inventory(payload):
    # Never construct a network client. dump() is the only permitted operation.
    from types import SimpleNamespace

    backend = object.__new__(BambuLabsBackend)
    backend._printer = SimpleNamespace(
        mqtt_client=SimpleNamespace(dump=lambda: {"print": {"ams": payload}})
    )
    return backend._read_ams_inventory()


@pytest.mark.parametrize("extra", [{}, {"n": 0}, {"n": 1}])
def test_partial_tray_fields_and_optional_presence_bits(extra):
    units, trays = _inventory({"ams": [{"id": "0", "tray": [
        {"id": "0", "tray_type": "PLA", "remain": "42", **extra},
        {"id": "1"},
    ]}]})
    assert units == [0]
    assert len(trays) == 1
    assert trays[0].tray_id == 0
    assert trays[0].remaining_percent == 42
    assert trays[0].nozzle_temp_min is None


def test_empty_units_ht_ids_and_secret_fields():
    units, trays = _inventory({"ams": [
        {"id": "1", "tray": [{"id": "0"}, {"id": "1"}]},
        {"id": "128", "tray": [{
            "id": "0", "tray_type": "PETG", "remain": -1,
            "tray_uuid": "secret-uuid", "tag_uid": "secret-tag",
            "nozzle_temp_min": "nan", "nozzle_temp_max": "inf",
        }]},
        {"id": "129", "tray": [{"id": "0", "tray_type": "PLA"}]},
    ]})
    assert units == [1, 128, 129]
    assert [(tray.ams_id, tray.tray_id) for tray in trays] == [(128, 0), (129, 0)]
    assert trays[0].remaining_percent is None
    assert trays[0].nozzle_temp_min is None
    assert trays[0].nozzle_temp_max is None
    assert "secret" not in repr(trays)


@pytest.mark.parametrize("payload", [None, {}, {"ams": None}, {"ams": [None]},
    {"ams": [{"id": "bad", "tray": []}]},
    {"ams": [{"id": "0.5", "tray": []}]},
    {"ams": [{"id": "0", "tray": [{"id": "bad"}]}]},
    {"ams": [{"id": "0", "tray": [{"id": "0"}, {"id": "0"}]}]},
])
def test_unknown_or_malformed_inventory_is_not_reported_as_empty(payload):
    assert _inventory(payload) == (None, None)


def test_known_empty_inventory_is_distinct_from_unknown():
    assert _inventory({"ams": [{"id": "1", "tray": [{"id": "0"}]}]}) == ([1], [])
    assert _inventory({"ams": []}) == ([], [])
