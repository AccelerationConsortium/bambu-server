"""Agent mode: admin-only switch, A1-only scope, refusals, and agent_camera starts.

Every printer is a :class:`FakeBackend`; nothing here can reach a printer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bambu_server.config import PrinterDefinition, Settings
from bambu_server.dispatch import Dispatcher
from bambu_server.main import create_app

from .conftest import SAMPLE_GCODE, FakeBackend, write_3mf

A1 = "bambu_a1_01"
X1 = "bambu_test_01"
EDGE = "test-edge-secret"

ADMIN = {"X-Edge-Auth": EDGE, "X-Auth-User": "yang", "X-Auth-Role": "admin"}
OPERATOR = {"X-Edge-Auth": EDGE, "X-Auth-User": "alice", "X-Auth-Role": "operator"}
AGENT = {
    "X-Edge-Auth": EDGE,
    "X-Auth-User": "svc-print-agent",
    "X-Auth-Role": "agent",
    "X-Agent-Id": "planner-7",
}


def _settings(settings: Settings, monkeypatch, *, first_model: str = "X1 Carbon") -> Settings:
    monkeypatch.setenv("BAMBU_EDGE_SHARED_SECRET", EDGE)
    for key in ("HOST", "ACCESS_CODE", "SERIAL"):
        monkeypatch.setenv(f"BAMBU_A1_01_{key}", "secret")
    payload = settings.model_dump(mode="json")
    payload["poll_interval_seconds"] = 0.5
    payload["stale_after_seconds"] = 5.0
    first = payload["printers"][0]
    first["model"] = first_model
    first["profile"]["plate"] = "textured_plate"
    a1 = {
        **first,
        "id": A1,
        "name": "Bambu A1 01",
        "model": "A1",
        "env_prefix": "BAMBU_A1_01",
        "agent_mode_eligible": True,
        "profile": {**first["profile"], "enclosure": "open"},
    }
    payload["printers"].append(a1)
    payload["dispatch"] = {"enabled": True, "require_verified_identity": True}
    built = Settings.model_validate(payload)
    built.dispatch = built.dispatch.model_copy(update={"confirm_timeout_s": 2.0})
    return built


@pytest.fixture
def backends(reading) -> dict[str, FakeBackend]:
    made = {X1: FakeBackend(reading), A1: FakeBackend(reading)}
    for backend in made.values():
        backend.refresh_timestamps = True
    return made


@pytest.fixture
def dispatchers(monkeypatch) -> list[Dispatcher]:
    created: list[Dispatcher] = []
    original = Dispatcher.__init__

    def capture(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(Dispatcher, "__init__", capture)
    return created


@pytest.fixture
def client(settings: Settings, backends, monkeypatch, dispatchers) -> Iterator[TestClient]:
    app = create_app(
        settings=_settings(settings, monkeypatch),
        backend_factory=lambda definition, _c: backends[definition.id],
    )
    with TestClient(app) as test_client:
        yield test_client


def _submit(client: TestClient, tmp_path: Path, printer: str, headers: dict) -> object:
    body = SAMPLE_GCODE.replace("X1 Carbon", "A1") if printer == A1 else SAMPLE_GCODE
    artifact = write_3mf(tmp_path / f"{printer}-{len(list(tmp_path.iterdir()))}.3mf",
                         plate_gcode=body)
    return client.post(
        "/submissions",
        files={"file": ("cube.3mf", artifact.read_bytes(), "application/octet-stream")},
        data={"target_machine": printer},
        headers=headers,
    )


def _flip(client: TestClient, printer: str, enabled: bool, headers=ADMIN, reason="test run"):
    return client.post(
        f"/printers/{printer}/agent-mode",
        json={"enabled": enabled, "reason": reason},
        headers=headers,
    )


def _claim(client: TestClient, printer: str, headers: dict, session: str = "s1") -> str:
    response = client.post(
        f"/printers/{printer}/control/claim",
        json={"owner": "x", "session_id": session},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["claim_token"]


def _agent_start_body(sid: str, **overrides) -> dict:
    body = {
        "submission_id": sid,
        "plate_confirmed_empty": True,
        "plate_check_method": "agent_camera",
        "ams_mapping": [{"filament_id": 1, "ams_id": 0, "tray_id": 1}],
        "plate_check_rationale": "plate matches the empty reference; no residue",
    }
    body.update(overrides)
    return body


# -- the switch -------------------------------------------------------------------


def test_only_an_edge_verified_admin_can_flip_agent_mode(client: TestClient) -> None:
    assert _flip(client, A1, True, headers={}).status_code == 403
    assert _flip(client, A1, True, headers={"X-Auth-User": "yang", "X-Auth-Role": "admin"}
                 ).status_code == 403  # forged, no edge secret
    assert _flip(client, A1, True, headers=OPERATOR).status_code == 403
    assert _flip(client, A1, True, headers={**ADMIN, "X-Agent-Id": "planner-7"}).status_code == 403
    assert client.get(f"/printers/{A1}/agent-mode").json()["enabled"] is False
    assert client.post(f"/printers/{A1}/agent-mode", json={"enabled": True},
                       headers=ADMIN).status_code == 422  # a reason is required

    response = _flip(client, A1, True, reason="supervised A1 trial")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["enabled"] is True
    assert view["changed_by"] == "yang"
    assert view["reason"] == "supervised A1 trial"
    assert view["audit"][0]["changed_by"] == "yang"
    assert view["audit"][0]["changed_by_role"] == "admin"
    assert view["audit"][0]["previous"] is False

    status = client.get(f"/printers/{A1}/status").json()["details"]["agent_mode"]
    assert status["eligible"] is True and status["enabled"] is True
    assert status["changed_by"] == "yang"
    assert status["changed_at"]
    assert status["reason"] == "supervised A1 trial"
    gateway = client.get("/status").json()["details"]["agent_mode"]
    assert gateway[A1]["enabled"] is True and gateway[X1]["eligible"] is False

    _flip(client, A1, False, reason="trial over")
    audit = client.get(f"/printers/{A1}/agent-mode").json()["audit"]
    assert [entry["enabled"] for entry in audit] == [False, True]


def test_the_switch_and_its_audit_survive_a_restart(
    settings: Settings, backends, monkeypatch
) -> None:
    built = _settings(settings, monkeypatch)
    factory = lambda definition, _c: backends[definition.id]  # noqa: E731
    with TestClient(create_app(settings=built, backend_factory=factory)) as client:
        _flip(client, A1, True, reason="persist me")
    with TestClient(create_app(settings=built, backend_factory=factory)) as client:
        view = client.get(f"/printers/{A1}/agent-mode").json()
        assert view["enabled"] is True
        assert view["audit"][0]["reason"] == "persist me"


# -- A1-only scope -----------------------------------------------------------------


def test_agent_mode_cannot_be_switched_on_for_any_other_printer(client: TestClient) -> None:
    response = _flip(client, X1, True)
    assert response.status_code == 409
    assert "Bambu A1 only" in response.json()["detail"]
    assert client.get(f"/printers/{X1}/agent-mode").json()["eligible"] is False


@pytest.mark.parametrize("model", ["X1 Carbon", "P1S", "H2D", "A1 mini", None])
def test_config_accepts_agent_mode_eligibility_on_an_a1_only(model: str | None) -> None:
    with pytest.raises(ValidationError, match="Bambu A1"):
        PrinterDefinition(
            id="p", name="P", model=model, env_prefix="P", agent_mode_eligible=True
        )
    PrinterDefinition(id="a", name="A", model="Bambu Lab A1", env_prefix="A",
                      agent_mode_eligible=True)


def test_agents_are_refused_on_other_printers_even_with_the_a1_on(
    client: TestClient, tmp_path: Path, backends
) -> None:
    _flip(client, A1, True)

    refused = _submit(client, tmp_path, X1, AGENT)
    assert refused.status_code == 403
    assert refused.json()["detail"]["agent_mode"] == "not_eligible"

    human = _submit(client, tmp_path, X1, OPERATOR).json()
    approve = client.post(f"/submissions/{human['submission_id']}/approve", json={},
                          headers=AGENT)
    assert approve.status_code == 403
    assert approve.json()["detail"]["agent_mode"] == "not_eligible"

    client.post(f"/submissions/{human['submission_id']}/approve", json={}, headers=OPERATOR)
    token = _claim(client, X1, AGENT)
    start = client.post(
        f"/printers/{X1}/control/start_print",
        headers={**AGENT, "X-Claim-Token": token},
        json=_agent_start_body(human["submission_id"], snapshot_token="t"),
    )
    assert start.status_code == 403
    assert start.json()["agent_mode"] == "not_eligible"
    assert backends[X1].uploads == [] and backends[X1].start_commands == []


# -- refusals with the switch off ------------------------------------------------------


def test_with_the_switch_off_agent_submission_approval_and_start_are_refused(
    client: TestClient, tmp_path: Path, backends
) -> None:
    refused = _submit(client, tmp_path, A1, AGENT)
    assert refused.status_code == 403
    assert refused.json()["detail"]["agent_mode"] == "off"
    assert "agent mode is off" in refused.json()["detail"]["detail"]

    human = _submit(client, tmp_path, A1, OPERATOR)
    assert human.status_code == 201
    sid = human.json()["submission_id"]
    approve = client.post(f"/submissions/{sid}/approve", json={}, headers=AGENT)
    assert approve.status_code == 403
    assert approve.json()["detail"]["agent_mode"] == "off"

    client.post(f"/submissions/{sid}/approve", json={}, headers=OPERATOR)
    token = _claim(client, A1, AGENT)
    snap = client.post(f"/printers/{A1}/control/snapshot",
                       headers={**AGENT, "X-Claim-Token": token}).json()
    start = client.post(
        f"/printers/{A1}/control/start_print",
        headers={**AGENT, "X-Claim-Token": token},
        json=_agent_start_body(sid, snapshot_token=snap["token"]),
    )
    assert start.status_code == 403
    assert start.json()["agent_mode"] == "off"
    assert backends[A1].uploads == [] and backends[A1].start_commands == []


def test_the_human_path_is_unchanged_with_the_switch_off(
    client: TestClient, tmp_path: Path, backends
) -> None:
    sid = _submit(client, tmp_path, A1, OPERATOR).json()["submission_id"]
    client.post(f"/submissions/{sid}/approve", json={}, headers=OPERATOR)
    token = _claim(client, A1, OPERATOR)
    start = client.post(
        f"/printers/{A1}/control/start_print",
        headers={**OPERATOR, "X-Claim-Token": token},
        json={
            "submission_id": sid,
            "plate_confirmed_empty": True,
            "plate_check_method": "in_person",
            "ams_mapping": [{"filament_id": 1, "ams_id": 0, "tray_id": 1}],
        },
    )
    assert start.status_code == 200, start.text
    job = start.json()
    assert job["state"] == "running"
    assert job["dispatch"]["plate_check"]["decided_by"] == "person"
    assert job["dispatch"]["dispatched_by_agent"] is None


def test_a_person_cannot_use_the_agents_plate_check(
    client: TestClient, tmp_path: Path, backends
) -> None:
    _flip(client, A1, True)
    sid = _submit(client, tmp_path, A1, OPERATOR).json()["submission_id"]
    client.post(f"/submissions/{sid}/approve", json={}, headers=OPERATOR)
    token = _claim(client, A1, OPERATOR)
    start = client.post(
        f"/printers/{A1}/control/start_print",
        headers={**OPERATOR, "X-Claim-Token": token},
        json=_agent_start_body(sid, snapshot_token="any"),
    )
    assert start.status_code == 403
    assert backends[A1].start_commands == []


# -- the agent flow on the A1 -------------------------------------------------------------


def test_an_agent_runs_the_a1_end_to_end_on_its_own_camera_decision(
    client: TestClient, tmp_path: Path, backends
) -> None:
    _flip(client, A1, True, reason="supervised trial")

    created = _submit(client, tmp_path, A1, AGENT)
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["state"] == "queued"
    assert job["requested_by"] == "svc-print-agent"
    assert job["requested_by_verified"] is True
    assert job["requested_by_agent"] == "planner-7"
    sid = job["submission_id"]

    approved = client.post(f"/submissions/{sid}/approve", json={}, headers=AGENT).json()
    assert approved["state"] == "approved"
    assert approved["approved_by_agent"] == "planner-7"

    headers = {**AGENT, "X-Claim-Token": _claim(client, A1, AGENT)}
    snap = client.post(f"/printers/{A1}/control/snapshot", headers=headers)
    assert snap.status_code == 200, snap.text
    snapshot = snap.json()
    assert snapshot["image_base64"] and snapshot["token"]

    start = client.post(
        f"/printers/{A1}/control/start_print",
        headers=headers,
        json=_agent_start_body(sid, snapshot_token=snapshot["token"]),
    )
    assert start.status_code == 200, start.text
    record = start.json()["dispatch"]
    assert start.json()["state"] == "running"
    check = record["plate_check"]
    assert check["method"] == "agent_camera"
    assert check["decided_by"] == "agent"
    assert check["agent_id"] == "planner-7"
    assert check["confirmed_by"] == "svc-print-agent"
    assert check["rationale"] == "plate matches the empty reference; no residue"
    assert check["snapshot_sha256"] == snapshot["sha256"]
    assert record["dispatched_by_agent"] == "planner-7"
    assert len(backends[A1].start_commands) == 1

    evidence = client.get(f"/submissions/{sid}/plate.jpg")
    assert evidence.status_code == 200
    assert hashlib.sha256(evidence.content).hexdigest() == snapshot["sha256"]


def test_an_agent_must_decide_from_a_fresh_snapshot_under_its_claim(
    client: TestClient, tmp_path: Path, backends, dispatchers
) -> None:
    _flip(client, A1, True)
    sid = _submit(client, tmp_path, A1, AGENT).json()["submission_id"]
    client.post(f"/submissions/{sid}/approve", json={}, headers=AGENT)
    headers = {**AGENT, "X-Claim-Token": _claim(client, A1, AGENT)}
    url = f"/printers/{A1}/control/start_print"

    in_person = client.post(url, headers=headers,
                            json=_agent_start_body(sid, plate_check_method="in_person"))
    assert in_person.status_code == 412
    assert "agent_camera" in in_person.json()["detail"]

    no_token = client.post(url, headers=headers, json=_agent_start_body(sid))
    assert no_token.status_code == 412
    assert no_token.json()["snapshot_missing"] is True

    snap = client.post(f"/printers/{A1}/control/snapshot", headers=headers).json()
    dispatcher = dispatchers[0]
    dispatcher._snapshots[snap["token"]].captured_at -= timedelta(seconds=121)
    stale = client.post(url, headers=headers,
                        json=_agent_start_body(sid, snapshot_token=snap["token"]))
    assert stale.status_code == 412
    assert stale.json()["snapshot_max_age_s"] == 120.0

    other = client.post(f"/printers/{X1}/control/claim",
                        json={"owner": "x", "session_id": "s9"}, headers=OPERATOR).json()
    x1_snap = client.post(f"/printers/{X1}/control/snapshot",
                          headers={**OPERATOR, "X-Claim-Token": other["claim_token"]}).json()
    wrong_printer = client.post(url, headers=headers,
                                json=_agent_start_body(sid, snapshot_token=x1_snap["token"]))
    assert wrong_printer.status_code == 412
    assert backends[A1].start_commands == []


def test_switching_agent_mode_off_refuses_the_next_agent_start(
    client: TestClient, tmp_path: Path, backends
) -> None:
    _flip(client, A1, True)
    sid = _submit(client, tmp_path, A1, AGENT).json()["submission_id"]
    client.post(f"/submissions/{sid}/approve", json={}, headers=AGENT)
    headers = {**AGENT, "X-Claim-Token": _claim(client, A1, AGENT)}
    snap = client.post(f"/printers/{A1}/control/snapshot", headers=headers).json()

    _flip(client, A1, False, reason="stop agents")
    start = client.post(
        f"/printers/{A1}/control/start_print", headers=headers,
        json=_agent_start_body(sid, snapshot_token=snap["token"]),
    )
    assert start.status_code == 403
    assert start.json()["agent_mode"] == "off"
    assert backends[A1].uploads == []


# -- H2D camera -----------------------------------------------------------------------------


def test_h2d_camera_snapshots_are_refused_explicitly(
    settings: Settings, backends, monkeypatch
) -> None:
    built = _settings(settings, monkeypatch, first_model="H2D")
    app = create_app(settings=built, backend_factory=lambda d, _c: backends[d.id])
    with TestClient(app) as client:
        token = _claim(client, X1, OPERATOR)
        response = client.post(f"/printers/{X1}/control/snapshot",
                               headers={**OPERATOR, "X-Claim-Token": token})
        assert response.status_code == 501
        assert "not supported" in response.json()["detail"]
        assert client.get(f"/printers/{X1}/camera.jpg").status_code == 503
    assert backends[X1].snapshots == 0
