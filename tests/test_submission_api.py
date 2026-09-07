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
            # delete exists only for retention housekeeping on finished jobs.
            assert method.lower() in {"get", "post", "delete"}, (path, method)


def test_reads_never_cause_printer_io(client: TestClient, backend: FakeBackend) -> None:
    before = backend.read_count
    client.get("/printers/bambu_test_01/profile")
    client.get("/printers/bambu_test_01/queue")
    client.get("/submissions")

    assert backend.read_count == before


def test_cancelling_removes_a_job_from_the_queue(client: TestClient) -> None:
    created = _upload(client).json()
    assert len(client.get("/printers/bambu_test_01/queue").json()["queued"]) == 1

    response = client.post(
        f"/submissions/{created['submission_id']}/cancel",
        json={"cancelled_by": "lab-operator", "reason": "superseded"},
    )

    assert response.status_code == 200
    job = response.json()
    assert job["state"] == "cancelled"
    assert job["artifact_removed"] is True
    assert job["history"][-1]["note"] == "cancelled by lab-operator: superseded"
    assert client.get("/printers/bambu_test_01/queue").json()["queued"] == []
    # The record survives so the withdrawal stays auditable.
    assert client.get(f"/submissions/{created['submission_id']}").status_code == 200


def test_cancelling_an_approved_job_is_allowed(client: TestClient) -> None:
    created = _upload(client).json()
    client.post(
        f"/submissions/{created['submission_id']}/approve",
        json={"approved_by": "lab-operator"},
    )

    response = client.post(
        f"/submissions/{created['submission_id']}/cancel",
        json={"cancelled_by": "lab-operator"},
    )

    assert response.status_code == 200
    assert response.json()["verdict"]["dispatch_ready"] is False


def test_cancelling_twice_is_a_conflict(client: TestClient) -> None:
    created = _upload(client).json()
    path = f"/submissions/{created['submission_id']}/cancel"
    client.post(path, json={"cancelled_by": "lab-operator"})

    assert client.post(path, json={"cancelled_by": "lab-operator"}).status_code == 409


def test_cancelling_a_rejected_submission_is_a_conflict(client: TestClient) -> None:
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    created = _upload(client, body=body.encode()).json()

    response = client.post(
        f"/submissions/{created['submission_id']}/cancel",
        json={"cancelled_by": "lab-operator"},
    )
    assert response.status_code == 409


def test_cancelling_an_unknown_submission_is_404(client: TestClient) -> None:
    response = client.post(
        "/submissions/" + "0" * 32 + "/cancel", json={"cancelled_by": "lab-operator"}
    )
    assert response.status_code == 404


def test_a_cancelled_job_can_be_filtered_for(client: TestClient) -> None:
    created = _upload(client).json()
    client.post(
        f"/submissions/{created['submission_id']}/cancel",
        json={"cancelled_by": "lab-operator"},
    )

    assert len(client.get("/submissions", params={"state": "cancelled"}).json()) == 1
    assert client.get("/submissions", params={"state": "queued"}).json() == []


def test_the_ui_page_is_served(client: TestClient) -> None:
    response = client.get("/ui")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Submit a print" in response.text


def test_the_ui_page_loads_nothing_from_off_host(client: TestClient) -> None:
    """No CDN, no build step: the page must work on an isolated lab network."""
    body = client.get("/ui").text

    assert "//cdn" not in body
    for marker in ('src="http', "src='http", 'href="http', "href='http", "@import"):
        assert marker not in body, marker


def test_the_ui_page_only_calls_public_endpoints(client: TestClient) -> None:
    body = client.get("/ui").text
    paths = client.get("/openapi.json").json()["paths"]

    # The approve/cancel URLs are built by concatenation, so match the verbs.
    for called in ("/printers", "/submissions", "/queue", "/profile", '"approve"', '"cancel"'):
        assert called in body, called
    # It must not reach for anything that would actuate a printer.
    assert "/control/" not in body
    assert not any("/control/" in path for path in paths)


def test_the_ui_page_is_not_in_the_api_schema(client: TestClient) -> None:
    """It is a page, not part of the contract a machine client reads."""
    assert "/ui" not in client.get("/openapi.json").json()["paths"]


# --- edge-injected identity over HTTP ---------------------------------------

EDGE_SECRET = "test-edge-secret"


def _edge_client(settings: Settings, backend: FakeBackend, monkeypatch) -> TestClient:
    monkeypatch.setenv("BAMBU_EDGE_SHARED_SECRET", EDGE_SECRET)
    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    return TestClient(app)


def _edge_headers(user: str = "alice", role: str = "operator") -> dict[str, str]:
    return {"X-Edge-Auth": EDGE_SECRET, "X-Auth-User": user, "X-Auth-Role": role}


def test_whoami_reports_no_identity_by_default(client: TestClient) -> None:
    body = client.get("/whoami").json()

    assert body == {
        "user": None,
        "role": None,
        "verified": False,
        "identity_available": False,
    }


def test_whoami_reports_the_edge_identity(
    settings: Settings, backend: FakeBackend, monkeypatch
) -> None:
    with _edge_client(settings, backend, monkeypatch) as client:
        body = client.get("/whoami", headers=_edge_headers()).json()

    assert body == {
        "user": "alice",
        "role": "operator",
        "verified": True,
        "identity_available": True,
    }


def test_whoami_distinguishes_not_signed_in_from_cannot_tell(
    settings: Settings, backend: FakeBackend, monkeypatch
) -> None:
    """`identity_available` is what lets the page word itself honestly."""
    with _edge_client(settings, backend, monkeypatch) as client:
        body = client.get("/whoami").json()

    assert body["verified"] is False
    assert body["identity_available"] is True


def test_a_forged_identity_header_is_ignored(
    settings: Settings, backend: FakeBackend, monkeypatch
) -> None:
    """The port stays reachable on the tailnet, so a bare header proves nothing."""
    with _edge_client(settings, backend, monkeypatch) as client:
        body = client.get("/whoami", headers={"X-Auth-User": "admin"}).json()
        assert body["verified"] is False

        response = _upload_with(client, headers={"X-Auth-User": "admin"})
        job = response.json()

    assert job["requested_by"] == "remote-user-1"
    assert job["requested_by_verified"] is False


def _upload_with(client: TestClient, *, headers: dict[str, str], form: dict | None = None):
    data = {"target_machine": "bambu_test_01", "requested_by": "remote-user-1"}
    if form:
        data.update(form)
    return client.post(
        "/submissions",
        files={"file": ("part.gcode", SAMPLE_GCODE.encode(), "application/octet-stream")},
        data=data,
        headers=headers,
    )


def test_a_verified_identity_becomes_the_submitter(
    settings: Settings, backend: FakeBackend, monkeypatch
) -> None:
    with _edge_client(settings, backend, monkeypatch) as client:
        job = _upload_with(client, headers=_edge_headers()).json()

    # The form said remote-user-1; the signed-in account wins.
    assert job["requested_by"] == "alice"
    assert job["requested_by_verified"] is True


def test_approval_and_cancellation_record_the_verified_actor(
    settings: Settings, backend: FakeBackend, monkeypatch
) -> None:
    with _edge_client(settings, backend, monkeypatch) as client:
        first = _upload_with(client, headers=_edge_headers()).json()
        approved = client.post(
            f"/submissions/{first['submission_id']}/approve",
            json={"approved_by": "somebody-else"},
            headers=_edge_headers(user="bob"),
        ).json()

        second = _upload_with(client, headers=_edge_headers()).json()
        cancelled = client.post(
            f"/submissions/{second['submission_id']}/cancel",
            json={"reason": "not needed"},
            headers=_edge_headers(user="carol"),
        ).json()

    assert approved["approved_by"] == "bob"
    assert approved["approved_by_verified"] is True
    assert approved["history"][-1]["note"] == "approved by bob (verified identity)"

    assert cancelled["state"] == "cancelled"
    assert cancelled["history"][-1]["note"] == (
        "cancelled by carol (verified identity): not needed"
    )


def test_an_unverified_actor_is_marked_as_such(client: TestClient) -> None:
    created = _upload(client).json()
    approved = client.post(
        f"/submissions/{created['submission_id']}/approve",
        json={"approved_by": "lab-operator"},
    ).json()

    assert created["requested_by_verified"] is False
    assert approved["approved_by_verified"] is False
    assert approved["history"][-1]["note"] == "approved by lab-operator"


def test_a_name_is_required_when_no_identity_is_verified(client: TestClient) -> None:
    response = client.post(
        "/submissions",
        files={"file": ("part.gcode", SAMPLE_GCODE.encode(), "application/octet-stream")},
        data={"target_machine": "bambu_test_01"},
    )
    assert response.status_code == 422

    created = _upload(client).json()
    for path, _ in (("approve", None), ("cancel", None)):
        refused = client.post(f"/submissions/{created['submission_id']}/{path}", json={})
        assert refused.status_code == 422, path


def test_the_page_derives_its_api_base_from_its_own_url(client: TestClient) -> None:
    """One file must serve both the direct deployment and an edge path prefix."""
    body = client.get("/ui").text

    assert "API_BASE" in body
    assert 'window.location.pathname.replace' in body
    # No bare-rooted fetch: that would reach the dashboard behind the edge.
    assert 'fetch("/' not in body
    assert "/whoami" in body


def test_no_secret_is_ever_returned(
    settings: Settings, backend: FakeBackend, monkeypatch
) -> None:
    with _edge_client(settings, backend, monkeypatch) as client:
        for path in ("/whoami", "/", "/status", "/ui", "/submissions"):
            assert EDGE_SECRET not in client.get(path, headers=_edge_headers()).text, path


def test_the_ui_is_served_at_both_slash_spellings(client: TestClient) -> None:
    """Behind an edge prefix the canonical URL ends in a slash.

    Serving only `/ui` would make Starlette redirect `/ui/` to `/ui`, and that
    Location drops the edge prefix — landing the visitor on the dashboard
    instead of the page. Both spellings must answer directly.
    """
    for path in ("/ui", "/ui/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200, path
        assert "<title>Submit a print" in response.text


def test_a_finished_job_can_be_deleted_over_http(client: TestClient) -> None:
    created = _upload(client).json()
    sid = created["submission_id"]
    client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "lab-operator"})

    response = client.delete(f"/submissions/{sid}")

    assert response.status_code == 204
    assert client.get(f"/submissions/{sid}").status_code == 404
    assert client.get("/submissions").json() == []


def test_a_waiting_job_cannot_be_deleted(client: TestClient) -> None:
    """Withdrawing a queued job is `cancel`, which leaves a record."""
    created = _upload(client).json()

    response = client.delete(f"/submissions/{created['submission_id']}")

    assert response.status_code == 409
    assert "only a finished submission" in response.json()["detail"]
    assert client.get(f"/submissions/{created['submission_id']}").status_code == 200


def test_deleting_an_unknown_submission_is_404(client: TestClient) -> None:
    assert client.delete("/submissions/" + "0" * 32).status_code == 404
