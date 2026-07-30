from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from bambu_server.backend import PrinterReading
from bambu_server.config import Settings
from bambu_server.main import create_app

from .conftest import FakeBackend


def test_gateway_and_per_printer_probe(client: TestClient) -> None:
    assert client.get("/").json() == {
        "service": "ac-bambu-server",
        "version": "0.1.0",
        "mode": "monitoring_only",
        "printer_count": 1,
    }
    assert client.get("/health").json() == {"status": "healthy"}
    assert client.get("/printers").json() == [
        {
            "id": "bambu_test_01",
            "name": "Bambu Test 01",
            "model": "X1 Carbon",
            "status_path": "/printers/bambu_test_01/status",
        }
    ]
    assert client.get("/printers/bambu_test_01/").json() == {
        "equipment_id": "bambu_test_01",
        "equipment_name": "Bambu Test 01",
        "protocol_version": "1.2",
    }


def test_gateway_status_summarizes_printer_monitoring(client: TestClient) -> None:
    response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["equipment_id"] == "bambu_gateway"
    assert body["equipment_name"] == "Bambu Gateway"
    assert body["equipment_status"] == "ready"
    assert body["allowed_actions"] == []
    assert body["components"]["bambu_test_01"]["connected"] is True
    assert body["components"]["bambu_test_01"]["state"] == "ready"
    assert body["details"] == {
        "monitoring_only": True,
        "printer_count": 1,
    }


def test_ready_status_is_cached_and_contains_no_secrets(
    client: TestClient, backend: FakeBackend
) -> None:
    initial_reads = backend.read_count
    response = client.get("/printers/bambu_test_01/status")
    assert response.status_code == 200
    body = response.json()
    assert backend.read_count == initial_reads
    assert body["protocol_version"] == "1.2"
    assert body["equipment_kind"] == "other"
    assert body["equipment_status"] == "ready"
    assert body["allowed_actions"] == []
    assert body["components"]["mqtt"]["connected"] is True
    assert body["metrics"]["bed_temperature"]["unit"] == "C"
    assert body["metrics"]["remaining_time"]["unit"] == "min"
    serialized = response.text
    assert "secret-access-code" not in serialized
    assert "secret-serial" not in serialized
    assert "printer.invalid" not in serialized


def test_running_print_is_busy(settings: Settings) -> None:
    backend = FakeBackend(
        PrinterReading(
            data_updated_at=datetime.now(UTC),
            connected=True,
            data_ready=True,
            gcode_state="RUNNING",
            activity="PRINTING",
        )
    )
    app = create_app(settings=settings, backend_factory=lambda _definition, _creds: backend)
    with TestClient(app) as test_client:
        body = test_client.get("/printers/bambu_test_01/status").json()
    assert body["equipment_status"] == "busy"


def test_unreachable_and_stale_are_unknown(settings: Settings) -> None:
    for reading, expected_message in [
        (
            PrinterReading(data_updated_at=None, connected=False, data_ready=False),
            "Printer MQTT endpoint is unreachable",
        ),
        (
            PrinterReading(
                data_updated_at=datetime.now(UTC) - timedelta(minutes=10),
                connected=True,
                data_ready=True,
                gcode_state="IDLE",
            ),
            "Printer MQTT state is stale",
        ),
    ]:
        backend = FakeBackend(reading)
        app = create_app(
            settings=settings,
            backend_factory=lambda _definition, _creds, value=backend: value,
        )
        with TestClient(app) as test_client:
            body = test_client.get("/printers/bambu_test_01/status").json()
        assert body["equipment_status"] == "unknown"
        assert body["message"] == expected_message


def test_failed_print_is_error(settings: Settings) -> None:
    backend = FakeBackend(
        PrinterReading(
            data_updated_at=datetime.now(UTC),
            connected=True,
            data_ready=True,
            gcode_state="FAILED",
            activity="UNKNOWN",
        )
    )
    app = create_app(settings=settings, backend_factory=lambda _definition, _creds: backend)
    with TestClient(app) as test_client:
        body = test_client.get("/printers/bambu_test_01/status").json()
    assert body["equipment_status"] == "error"
    assert body["last_error"]["code"] == "print_failed"


def test_no_control_routes_are_exposed(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    assert not any("/control/" in path for path in document["paths"])


def test_unknown_printer_returns_404(client: TestClient) -> None:
    response = client.get("/printers/not_configured/status")
    assert response.status_code == 404
