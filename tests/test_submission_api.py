"""HTTP surface for the submission pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from bambu_server.backend import PrinterReading
from bambu_server.config import Settings
from bambu_server.main import create_app

from .conftest import SAMPLE_GCODE, FakeBackend, write_3mf


def _upload(
    client: TestClient,
    *,
    body: bytes | None = None,
    filename: str = "part.gcode",
    machine: str = "bambu_test_01",
    material: str | None = None,
):
    payload = SAMPLE_GCODE.encode() if body is None else body
    data = {"target_machine": machine, "requested_by": "remote-user-1"}
    if material is not None:
        data["material"] = material
    return client.post(
        "/submissions",
        files={"file": (filename, payload, "application/octet-stream")},
        data=data,
    )


def test_machine_profile_merges_declared_and_observed_fields(client: TestClient) -> None:
    body = client.get("/printers/bambu_test_01/profile").json()

    assert body["id"] == "bambu_test_01"
    assert body["bed_size_mm"] == [256.0, 256.0]
    assert body["enclosure"] == "enclosed"
    # The printer reports its own nozzle, so that is what a submitter is checked
    # against; the declared value is the fallback for a blank live field.
    assert body["nozzle_diameter_mm"] == 0.4
    assert body["nozzle_diameter_source"] == "observed"
    assert body["ams"]["filament_forbidden"] == ["ABS"]
    assert body["observed"]["loaded_trays"][0]["tray_type"] == "PLA"


def test_profile_falls_back_to_the_declared_nozzle_when_live_is_blank(
    settings: Settings,
) -> None:
    """A dual-nozzle H2D reports no parsable nozzle type; the profile still answers."""
    backend = FakeBackend(
        PrinterReading(
            data_updated_at=datetime.now(UTC),
            connected=True,
            data_ready=True,
            gcode_state="IDLE",
        )
    )
    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as test_client:
        body = test_client.get("/printers/bambu_test_01/profile").json()

    assert body["nozzle_diameter_mm"] == 0.4
    assert body["nozzle_diameter_source"] == "declared"
    assert body["nozzle_type_source"] == "declared"


def test_profile_for_an_unknown_printer_is_404(client: TestClient) -> None:
    assert client.get("/printers/not_configured/profile").status_code == 404


def test_a_conforming_submission_is_validated_and_queued(client: TestClient) -> None:
    response = _upload(client)

    assert response.status_code == 201
    job = response.json()
    assert job["state"] == "queued"
    assert job["target_machine"] == "bambu_test_01"
    assert job["requested_by"] == "remote-user-1"
    assert job["verdict"]["verdict"] == "pass"
    assert job["verdict"]["reasons"] == []
    assert job["verdict"]["dispatch_ready"] is False
    assert job["estimated_duration_minutes"] == 70.0
    assert job["facts"]["extent_mm"] == [100.0, 50.0, 0.0]


def test_a_submission_response_never_reveals_where_the_file_was_stored(
    client: TestClient, settings: Settings
) -> None:
    response = _upload(client)

    serialized = response.text
    assert str(settings.submissions.directory) not in serialized
    assert "/tmp" not in serialized
    assert response.json()["original_filename"] == "part.gcode"


def test_a_nonconforming_submission_is_rejected_with_reasons(client: TestClient) -> None:
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    response = _upload(client, body=body.encode())

    assert response.status_code == 201
    job = response.json()
    assert job["state"] == "rejected"
    assert job["verdict"]["verdict"] == "reject"
    assert job["verdict"]["reasons"] == ["machine_compatible"]


def test_a_forbidden_material_is_rejected_over_http(client: TestClient) -> None:
    body = SAMPLE_GCODE.replace("; filament_type = PLA", "; filament_type = ABS")
    job = _upload(client, body=body.encode()).json()

    assert job["state"] == "rejected"
    assert "material_allowed" in job["verdict"]["reasons"]


def test_a_declared_material_that_contradicts_the_model_is_rejected(
    client: TestClient,
) -> None:
    job = _upload(client, material="PETG").json()

    assert job["state"] == "rejected"
    assert "material_allowed" in job["verdict"]["reasons"]


def test_a_sliced_3mf_is_accepted(client: TestClient, tmp_path: Path) -> None:
    payload = write_3mf(tmp_path / "plate.3mf").read_bytes()
    job = _upload(client, body=payload, filename="plate.3mf").json()

    assert job["state"] == "queued"
    assert job["artifact_kind"] == "3mf"


def test_an_unsliced_3mf_is_rejected(client: TestClient, tmp_path: Path) -> None:
    payload = write_3mf(tmp_path / "project.3mf", plate_gcode=None).read_bytes()
    job = _upload(client, body=payload, filename="project.3mf").json()

    assert job["state"] == "rejected"
    assert "params_present" in job["verdict"]["reasons"]


def test_an_unknown_target_machine_is_404(client: TestClient) -> None:
    response = _upload(client, machine="bambu_nowhere")

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown target machine"


def test_an_unsupported_extension_is_400(client: TestClient) -> None:
    response = _upload(client, filename="model.stl")

    assert response.status_code == 400
    assert ".3mf" in response.json()["detail"]


def test_a_3mf_that_is_not_a_container_is_400(client: TestClient) -> None:
    """A malformed upload is refused at intake, not carried into validation."""
    response = _upload(client, body=b"not a zip at all", filename="fake.3mf")

    assert response.status_code == 400
    assert "not a valid 3mf container" in response.json()["detail"]


def test_an_oversized_upload_is_413(settings: Settings, backend: FakeBackend) -> None:
    settings.submissions.max_file_bytes = 1024
    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as test_client:
        response = _upload(test_client, body=b"G1 X1\n" * 4096)

    assert response.status_code == 413


def test_submissions_can_be_listed_and_fetched(client: TestClient) -> None:
    created = _upload(client).json()

    listing = client.get("/submissions").json()
    assert [job["submission_id"] for job in listing] == [created["submission_id"]]

    assert client.get("/submissions", params={"state": "queued"}).json() != []
    assert client.get("/submissions", params={"state": "approved"}).json() == []
    assert client.get("/submissions", params={"machine": "elsewhere"}).json() == []

    fetched = client.get(f"/submissions/{created['submission_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["submission_id"] == created["submission_id"]


def test_an_unknown_submission_is_404(client: TestClient) -> None:
    assert client.get("/submissions/" + "0" * 32).status_code == 404


def test_approval_records_sign_off_and_sets_dispatch_ready(client: TestClient) -> None:
    created = _upload(client).json()

    response = client.post(
        f"/submissions/{created['submission_id']}/approve",
        json={"approved_by": "lab-operator"},
    )

    assert response.status_code == 200
    job = response.json()
    assert job["state"] == "approved"
    assert job["approved_by"] == "lab-operator"
    assert job["verdict"]["dispatch_ready"] is True
    assert job["history"][-1]["note"] == "approved by lab-operator"


def test_approving_twice_is_a_conflict(client: TestClient) -> None:
    created = _upload(client).json()
    path = f"/submissions/{created['submission_id']}/approve"
    client.post(path, json={"approved_by": "lab-operator"})

    second = client.post(path, json={"approved_by": "lab-operator"})
    assert second.status_code == 409


def test_a_rejected_submission_cannot_be_approved(client: TestClient) -> None:
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    created = _upload(client, body=body.encode()).json()

    response = client.post(
        f"/submissions/{created['submission_id']}/approve",
        json={"approved_by": "lab-operator"},
    )
    assert response.status_code == 409


def test_the_queue_endpoint_reports_order_and_finish_times(client: TestClient) -> None:
    first = _upload(client).json()
    second = _upload(client).json()

    view = client.get("/printers/bambu_test_01/queue").json()

    assert view["machine"] == "bambu_test_01"
    assert view["running"] is None
    assert [job["submission_id"] for job in view["queued"]] == [
        first["submission_id"],
        second["submission_id"],
    ]
    assert view["estimates_complete"] is True
    assert view["queued"][0]["expected_end"] < view["queued"][1]["expected_end"]


def test_the_queue_reports_the_running_print(settings: Settings) -> None:
    backend = FakeBackend(
        PrinterReading(
            data_updated_at=datetime.now(UTC),
            connected=True,
            data_ready=True,
            gcode_state="RUNNING",
            activity="PRINTING",
            remaining_time_minutes=25,
            progress_percent=40,
            job_name="live_part.3mf",
        )
    )
    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as test_client:
        view = test_client.get("/printers/bambu_test_01/queue").json()

    assert view["activity"] == "running"
    assert view["running"]["job_name"] == "live_part.3mf"
    assert view["running"]["remaining_time_minutes"] == 25.0
    assert view["running"]["expected_end"] is not None


def test_a_rejected_submission_never_enters_the_queue(client: TestClient) -> None:
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    _upload(client, body=body.encode())

    assert client.get("/printers/bambu_test_01/queue").json()["queued"] == []


def test_the_pipeline_exposes_no_control_routes_and_no_dispatch(
    client: TestClient,
) -> None:
    """The submission surface must not have grown a way to reach a printer."""
    paths = client.get("/openapi.json").json()["paths"]

    assert not any("/control/" in path for path in paths)
    assert not any("dispatch" in path for path in paths)
    for path, operations in paths.items():
        for method in operations:
            assert method.lower() in {"get", "post"}, (path, method)


def test_reads_never_cause_printer_io(client: TestClient, backend: FakeBackend) -> None:
    before = backend.read_count
    client.get("/printers/bambu_test_01/profile")
    client.get("/printers/bambu_test_01/queue")
    client.get("/submissions")

    assert backend.read_count == before
