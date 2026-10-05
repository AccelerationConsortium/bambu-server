"""Control plane: claims, the dispatch gate, and what reaches the (fake) printer.

Every test runs against :class:`FakeBackend`; nothing here can contact a
printer. The assertions that matter most are the negative ones -- when a gate
closes, the fake records no upload and no start command.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bambu_server.config import Settings
from bambu_server.main import create_app

from .conftest import FakeBackend, write_3mf

PRINTER = "bambu_test_01"
BASE = f"/printers/{PRINTER}"


def _control_settings(settings: Settings, **dispatch: object) -> Settings:
    payload = settings.model_dump(mode="json")
    payload["poll_interval_seconds"] = 0.5
    payload["stale_after_seconds"] = 5.0
    payload["printers"][0]["profile"]["plate"] = "textured_plate"
    payload["dispatch"] = {"enabled": True, "require_verified_identity": False, **dispatch}
    built = Settings.model_validate(payload)
    # Below the config floor on purpose: a test should not wait 10 s.
    built.dispatch = built.dispatch.model_copy(update={"confirm_timeout_s": 2.0})
    return built


@pytest.fixture
def control_backend(backend: FakeBackend) -> FakeBackend:
    backend.refresh_timestamps = True
    return backend


@pytest.fixture
def control_client(settings: Settings, control_backend: FakeBackend) -> Iterator[TestClient]:
    app = create_app(
        settings=_control_settings(settings),
        backend_factory=lambda _d, _c: control_backend,
    )
    with TestClient(app) as client:
        yield client


def _approved_job(client: TestClient, tmp_path: Path) -> str:
    artifact = write_3mf(tmp_path / "cube.3mf")
    response = client.post(
        "/submissions",
        files={"file": ("cube.3mf", artifact.read_bytes(), "application/octet-stream")},
        data={"target_machine": PRINTER, "requested_by": "submitter"},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["state"] == "queued", job["verdict"]
    approved = client.post(
        f"/submissions/{job['submission_id']}/approve", json={"approved_by": "approver"}
    )
    assert approved.status_code == 200, approved.text
    return job["submission_id"]


def _claim(client: TestClient, session: str = "session-a") -> str:
    response = client.post(
        f"{BASE}/control/claim", json={"owner": "operator", "session_id": session}
    )
    assert response.status_code == 200, response.text
    return response.json()["claim_token"]


def _start_body(submission_id: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "submission_id": submission_id,
        "plate_confirmed_empty": True,
        "plate_check_method": "in_person",
        "ams_mapping": [{"filament_id": 1, "ams_id": 0, "tray_id": 1}],
        "dispatched_by": "operator",
    }
    body.update(overrides)
    return body


# -- surface ---------------------------------------------------------------------


def test_control_routes_exist_only_when_enabled(
    client: TestClient, control_client: TestClient
) -> None:
    assert not any("/control/" in p for p in client.get("/openapi.json").json()["paths"])
    paths = control_client.get("/openapi.json").json()["paths"]
    for verb in ("claim", "heartbeat", "release", "snapshot", "start_print", "stop_print"):
        assert f"/printers/{{printer_id}}/control/{verb}" in paths
    assert control_client.get("/").json()["mode"] == "control"


def test_status_publishes_claim_and_allowed_actions(control_client: TestClient) -> None:
    body = control_client.get(f"{BASE}/status").json()
    assert body["details"]["claimed_by"] is None
    assert body["details"]["monitoring_only"] is False
    assert set(body["allowed_actions"]) == {"snapshot", "start_print"}

    _claim(control_client)
    holder = control_client.get(f"{BASE}/status").json()["details"]["claimed_by"]
    assert holder["owner"] == "operator"


def test_status_reads_never_cause_printer_commands(
    control_client: TestClient, control_backend: FakeBackend
) -> None:
    for _ in range(3):
        control_client.get(f"{BASE}/status")
        control_client.get(f"{BASE}/queue")
    assert control_backend.uploads == []
    assert control_backend.start_commands == []
    assert control_backend.snapshots == 0


# -- claims ----------------------------------------------------------------------


def test_claim_conflict_heartbeat_and_release(control_client: TestClient) -> None:
    token = _claim(control_client, "session-a")

    taken = control_client.post(
        f"{BASE}/control/claim", json={"owner": "someone", "session_id": "session-b"}
    )
    assert taken.status_code == 409
    assert taken.json()["claimed_by"]["session_id"] == "session-a"

    beat = control_client.post(f"{BASE}/control/heartbeat", headers={"X-Claim-Token": token})
    assert beat.status_code == 200
    lost = control_client.post(f"{BASE}/control/heartbeat", headers={"X-Claim-Token": "nope"})
    assert lost.status_code == 401

    released = control_client.post(f"{BASE}/control/release", headers={"X-Claim-Token": token})
    assert released.status_code == 204
    # Idempotent.
    again = control_client.post(f"{BASE}/control/release", headers={"X-Claim-Token": token})
    assert again.status_code == 204
    _claim(control_client, "session-b")


def test_control_without_a_claim_is_locked(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    response = control_client.post(f"{BASE}/control/start_print", json=_start_body(job_id))
    assert response.status_code == 423
    snap = control_client.post(f"{BASE}/control/snapshot")
    assert snap.status_code == 423
    assert control_backend.uploads == [] and control_backend.snapshots == 0


# -- the happy path --------------------------------------------------------------


def test_camera_confirmed_dispatch_runs_and_finishes(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    token = _claim(control_client)
    headers = {"X-Claim-Token": token}

    snap = control_client.post(f"{BASE}/control/snapshot", headers=headers)
    assert snap.status_code == 200, snap.text
    snapshot = snap.json()

    response = control_client.post(
        f"{BASE}/control/start_print",
        headers=headers,
        json=_start_body(
            job_id, plate_check_method="printer_camera", snapshot_token=snapshot["token"]
        ),
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["state"] == "running"
    record = job["dispatch"]
    assert record["plate_check"]["method"] == "printer_camera"
    assert record["plate_check"]["snapshot_sha256"] == snapshot["sha256"]
    assert record["upload_verified_bytes"] > 0
    assert record["observed_running_at"] is not None

    (command,) = control_backend.start_commands
    assert command.bed_type == "textured_plate"
    assert command.plate_index == 1
    assert command.ams_mapping == (1,)  # AMS 0, tray 1
    assert control_backend.uploads[0][0] == command.remote_name

    plate = control_client.get(f"/submissions/{job_id}/plate.jpg")
    assert plate.status_code == 200 and plate.content.startswith(b"\xff\xd8")

    # The printer finishes; the watcher records it.
    control_backend.reading = replace(control_backend.reading, gcode_state="FINISH")
    for _ in range(40):
        state = control_client.get(f"/submissions/{job_id}").json()["state"]
        if state == "finished":
            break
        time.sleep(0.1)
    assert state == "finished"


def test_a_job_is_dispatched_at_most_once(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    headers = {"X-Claim-Token": _claim(control_client)}
    first = control_client.post(
        f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id)
    )
    assert first.json()["state"] == "running"
    second = control_client.post(
        f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id)
    )
    assert second.status_code == 409
    assert len(control_backend.start_commands) == 1


# -- gates -----------------------------------------------------------------------


def test_hot_printer_is_refused_and_not_advertised(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    """allowed_actions and the 412 come from one predicate (STATUS_SPEC §6.2)."""
    job_id = _approved_job(control_client, tmp_path)
    control_backend.reading = replace(control_backend.reading, bed_temperature_c=60.0)
    time.sleep(0.7)  # let the monitor observe it
    assert "start_print" not in control_client.get(f"{BASE}/status").json()["allowed_actions"]

    headers = {"X-Claim-Token": _claim(control_client)}
    response = control_client.post(
        f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id)
    )
    assert response.status_code == 412
    assert response.json()["limit_bed_c"] == 45.0
    assert response.headers["Retry-After"] == "60"
    assert control_backend.uploads == [] and control_backend.start_commands == []


def test_wrong_tray_is_refused(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    headers = {"X-Claim-Token": _claim(control_client)}
    response = control_client.post(
        f"{BASE}/control/start_print",
        headers=headers,
        json=_start_body(job_id, ams_mapping=[{"filament_id": 1, "ams_id": 0, "tray_id": 3}]),
    )
    assert response.status_code == 412
    assert "reports no loaded filament" in response.json()["problems"][0]
    assert control_backend.uploads == []


def test_camera_check_needs_a_snapshot(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    headers = {"X-Claim-Token": _claim(control_client)}
    response = control_client.post(
        f"{BASE}/control/start_print",
        headers=headers,
        json=_start_body(job_id, plate_check_method="printer_camera", snapshot_token="made-up"),
    )
    assert response.status_code == 412
    assert response.json()["snapshot_missing"] is True
    assert control_backend.uploads == []


def test_plate_confirmation_must_be_affirmative(
    control_client: TestClient, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    headers = {"X-Claim-Token": _claim(control_client)}
    response = control_client.post(
        f"{BASE}/control/start_print",
        headers=headers,
        json=_start_body(job_id, plate_confirmed_empty=False),
    )
    assert response.status_code == 422


def test_a_job_sliced_for_another_plate_is_refused(
    settings: Settings, control_backend: FakeBackend, tmp_path: Path
) -> None:
    built = _control_settings(settings)
    built.printers[0].profile.plate = "cool_plate"
    app = create_app(settings=built, backend_factory=lambda _d, _c: control_backend)
    with TestClient(app) as client:
        job_id = _approved_job(client, tmp_path)
        headers = {"X-Claim-Token": _claim(client)}
        response = client.post(
            f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id)
        )
    assert response.status_code == 412
    assert response.json()["machine_plate"] == "cool_plate"
    assert control_backend.uploads == []


def test_verified_identities_are_required_by_default(
    settings: Settings, control_backend: FakeBackend, tmp_path: Path
) -> None:
    built = _control_settings(settings, require_verified_identity=True)
    app = create_app(settings=built, backend_factory=lambda _d, _c: control_backend)
    with TestClient(app) as client:
        job_id = _approved_job(client, tmp_path)
        headers = {"X-Claim-Token": _claim(client)}
        response = client.post(
            f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id)
        )
    assert response.status_code == 403
    assert control_backend.uploads == []


# -- honest failures -------------------------------------------------------------


def test_a_failed_upload_starts_nothing(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    control_backend.fail_upload = True
    headers = {"X-Claim-Token": _claim(control_client)}
    job = control_client.post(
        f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id)
    ).json()
    assert job["state"] == "failed"
    assert control_backend.start_commands == []
    assert "nothing was started" in job["history"][-1]["note"]


def test_a_start_the_printer_never_acts_on_fails_with_uncertainty(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    job_id = _approved_job(control_client, tmp_path)
    control_backend.start_runs = False
    headers = {"X-Claim-Token": _claim(control_client)}
    job = control_client.post(
        f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id)
    ).json()
    assert job["state"] == "failed"
    assert "real state is unknown" in job["dispatch"]["uncertainty"]
    assert len(control_backend.start_commands) == 1  # never retried


def test_stop_requires_a_running_job_and_a_claim(
    control_client: TestClient, control_backend: FakeBackend, tmp_path: Path
) -> None:
    headers = {"X-Claim-Token": _claim(control_client)}
    idle = control_client.post(
        f"{BASE}/control/stop_print", headers=headers, json={"stopped_by": "op"}
    )
    assert idle.status_code == 412
    assert control_backend.stop_calls == 0

    job_id = _approved_job(control_client, tmp_path)
    control_client.post(f"{BASE}/control/start_print", headers=headers, json=_start_body(job_id))
    locked = control_client.post(f"{BASE}/control/stop_print", json={"stopped_by": "op"})
    assert locked.status_code == 423
    stopped = control_client.post(
        f"{BASE}/control/stop_print", headers=headers, json={"stopped_by": "op", "reason": "test"}
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["submission_id"] == job_id
    assert control_backend.stop_calls == 1
