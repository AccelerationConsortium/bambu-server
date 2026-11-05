"""position_bed: the A1-only bed move (approved 2026-11-05).

Every printer is a :class:`FakeBackend`; nothing here can reach a printer. The
assertions that matter most are the negative ones -- when a gate closes, the
fake records no bed move.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bambu_server.backend import BambuLabsBackend, PrinterCommandError
from bambu_server.config import PrinterDefinition, Settings
from bambu_server.main import create_app

from .conftest import FakeBackend

A1 = "bambu_a1_01"
X1 = "bambu_test_01"
EDGE = "test-edge-secret"
OPERATOR = {"X-Edge-Auth": EDGE, "X-Auth-User": "alice", "X-Auth-Role": "operator"}
ADMIN = {"X-Edge-Auth": EDGE, "X-Auth-User": "yang", "X-Auth-Role": "admin"}
AGENT = {
    "X-Edge-Auth": EDGE, "X-Auth-User": "svc-print-agent", "X-Auth-Role": "agent",
    "X-Agent-Id": "planner-7",
}
POSITIONS = {"inspect_y_mm": 128, "pickup_y_mm": 250}


def _settings(settings: Settings, monkeypatch) -> Settings:
    monkeypatch.setenv("BAMBU_EDGE_SHARED_SECRET", EDGE)
    for key in ("HOST", "ACCESS_CODE", "SERIAL"):
        monkeypatch.setenv(f"BAMBU_A1_01_{key}", "secret")
    payload = settings.model_dump(mode="json")
    payload["poll_interval_seconds"] = 0.5
    payload["stale_after_seconds"] = 5.0
    first = payload["printers"][0]
    first["model"] = "X1 Carbon"
    first["profile"]["plate"] = "textured_plate"
    payload["printers"].append({
        **first, "id": A1, "name": "Bambu A1 01", "model": "A1", "env_prefix": "BAMBU_A1_01",
        "agent_mode_eligible": True, "bed_positions": POSITIONS,
        "profile": {**first["profile"], "enclosure": "open"},
    })
    payload["dispatch"] = {"enabled": True, "require_verified_identity": True}
    return Settings.model_validate(payload)


@pytest.fixture
def backends(reading) -> dict[str, FakeBackend]:
    made = {X1: FakeBackend(reading), A1: FakeBackend(reading)}
    for backend in made.values():
        backend.refresh_timestamps = True
    return made


@pytest.fixture
def client(settings: Settings, backends, monkeypatch) -> Iterator[TestClient]:
    app = create_app(
        settings=_settings(settings, monkeypatch),
        backend_factory=lambda definition, _c: backends[definition.id],
    )
    with TestClient(app) as test_client:
        yield test_client


def _claim(client: TestClient, printer: str, headers: dict) -> dict:
    response = client.post(
        f"/printers/{printer}/control/claim",
        json={"owner": "x", "session_id": "s1"}, headers=headers,
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Claim-Token": response.json()["claim_token"]}


def _move(client: TestClient, headers: dict, printer: str = A1, **body: object):
    payload = {"position": "inspect", "bed_path_clear": True, **body}
    return client.post(f"/printers/{printer}/control/position_bed", json=payload, headers=headers)


def test_a_person_moves_the_a1_bed_to_a_named_position(
    client: TestClient, backends: dict[str, FakeBackend]
) -> None:
    headers = _claim(client, A1, OPERATOR)
    assert "position_bed" in client.get(f"/printers/{A1}/status").json()["allowed_actions"]

    inspect = _move(client, headers, reason="camera check before the next start")
    assert inspect.status_code == 200, inspect.text
    assert inspect.json()["y_mm"] == 128
    assert inspect.json()["moved_by"] == "alice" and inspect.json()["moved_by_verified"]
    pickup = _move(client, headers, position="pickup")
    assert pickup.status_code == 200, pickup.text

    assert backends[A1].bed_moves == [(128, 3000), (250, 3000)]
    backend = backends[A1]
    assert backend.uploads == [] and backend.start_commands == [] and backend.stop_calls == 0
    events = [e for e in client.get("/events").json()["events"] if e["type"] == "bed.moved"]
    assert [e["printer_id"] for e in events] == [A1, A1]
    assert "inspect (Y128) by alice" in events[0]["note"]
    assert "camera check" in events[0]["note"]


def test_a_move_needs_the_claim(client: TestClient, backends: dict[str, FakeBackend]) -> None:
    response = _move(client, OPERATOR)
    assert response.status_code == 423
    assert backends[A1].bed_moves == []


@pytest.mark.parametrize(
    "body",
    [
        {"bed_path_clear": False},
        {"bed_path_clear": None},
        {"position": "home"},
        {"position": "inspect", "y_mm": 10},  # extra field ignored, but no raw Y is accepted
    ],
)
def test_only_a_named_position_with_a_clear_path_is_accepted(
    client: TestClient, backends: dict[str, FakeBackend], body: dict
) -> None:
    headers = _claim(client, A1, OPERATOR)
    response = _move(client, headers, **body)
    if "y_mm" in body:
        assert response.status_code == 200
        assert backends[A1].bed_moves == [(128, 3000)]  # the configured Y, not the caller's
    else:
        assert response.status_code == 422
        assert backends[A1].bed_moves == []


def test_an_unverified_caller_is_refused(
    client: TestClient, backends: dict[str, FakeBackend]
) -> None:
    claim = _claim(client, A1, OPERATOR)
    response = _move(client, {"X-Claim-Token": claim["X-Claim-Token"]}, moved_by="mallory")
    assert response.status_code == 403
    assert backends[A1].bed_moves == []


def test_the_bed_does_not_move_on_a_busy_printer(
    client: TestClient, backends: dict[str, FakeBackend]
) -> None:
    backends[A1].reading = replace(backends[A1].reading, gcode_state="RUNNING", activity="PRINTING")
    client.get(f"/printers/{A1}/status")
    headers = _claim(client, A1, OPERATOR)
    time.sleep(0.7)  # let the monitor see the new state
    assert "position_bed" not in client.get(f"/printers/{A1}/status").json()["allowed_actions"]
    response = _move(client, headers)
    assert response.status_code == 412
    assert "idle" in response.json()["detail"]
    assert backends[A1].bed_moves == []


def test_another_printer_has_no_bed_move(
    client: TestClient, backends: dict[str, FakeBackend]
) -> None:
    headers = _claim(client, X1, OPERATOR)
    assert "position_bed" not in client.get(f"/printers/{X1}/status").json()["allowed_actions"]
    response = _move(client, headers, printer=X1)
    assert response.status_code == 412
    assert "Bambu A1 only" in response.json()["detail"]
    assert backends[X1].bed_moves == []


def test_an_agent_moves_the_bed_only_in_agent_mode(
    client: TestClient, backends: dict[str, FakeBackend]
) -> None:
    headers = _claim(client, A1, AGENT)
    off = _move(client, headers)
    assert off.status_code == 403 and off.json()["agent_mode"] == "off"
    assert backends[A1].bed_moves == []

    flipped = client.post(
        f"/printers/{A1}/agent-mode", json={"enabled": True, "reason": "bed move test"},
        headers=ADMIN,
    )
    assert flipped.status_code == 200, flipped.text
    on = _move(client, headers)
    assert on.status_code == 200, on.text
    assert on.json()["agent_id"] == "planner-7"
    assert backends[A1].bed_moves == [(128, 3000)]


def test_an_undelivered_move_is_reported_not_retried(
    client: TestClient, backends: dict[str, FakeBackend]
) -> None:
    backends[A1].fail_bed_move = True
    headers = _claim(client, A1, OPERATOR)
    response = _move(client, headers)
    assert response.status_code == 502
    assert "do not retry" in response.json()["detail"]
    assert backends[A1].bed_moves == []
    assert not [e for e in client.get("/events").json()["events"] if e["type"] == "bed.moved"]


def test_bed_positions_are_refused_off_the_a1() -> None:
    with pytest.raises(ValidationError, match="Bambu A1 printers only"):
        PrinterDefinition(
            id="p", name="P", model="P1S", env_prefix="P", bed_positions=POSITIONS
        )
    with pytest.raises(ValidationError):
        PrinterDefinition(
            id="p", name="P", model="A1", env_prefix="P",
            bed_positions={"inspect_y_mm": 128, "pickup_y_mm": 300},
        )
    with pytest.raises(ValidationError, match="beyond"):
        PrinterDefinition(
            id="p", name="P", model="A1", env_prefix="P", bed_positions=POSITIONS,
            profile={"bed_size_mm": [256, 200]},
        )


def test_the_backend_sends_a_y_only_absolute_move() -> None:
    backend = BambuLabsBackend.__new__(BambuLabsBackend)
    sent: list[dict] = []
    backend._publish = sent.append  # type: ignore[method-assign]
    backend.move_bed_y(128.0, 3000)
    assert sent == [{"print": {
        "command": "gcode_line", "param": "G90\nG1 Y128.0 F3000\n", "sequence_id": "10000002",
    }}]
    gcode = sent[0]["print"]["param"]
    assert "X" not in gcode and "Z" not in gcode and "G28" not in gcode and "E" not in gcode
    for bad in ((-1.0, 3000), (257.0, 3000), (128.0, 10000)):
        with pytest.raises(PrinterCommandError):
            backend.move_bed_y(*bad)
    assert len(sent) == 1
