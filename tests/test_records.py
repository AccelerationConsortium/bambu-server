"""Durable result bundles, the central-ingest outbox, and lifecycle events.

Everything runs against fake printers and a fake central server; nothing here
can reach a printer, a robot, the dashboard or an ELN.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bambu_server.config import Settings
from bambu_server.main import create_app
from bambu_server.records import OutboxEntry, OutboxStore, OutboxWorker, ResultStore
from bambu_server.submissions import (
    DispatchRecord,
    PlateCheck,
    SubmissionStore,
    TrayAssignment,
)

from .conftest import SAMPLE_GCODE, FakeBackend, write_3mf
from .test_dispatch import BASE, PRINTER, _claim, _control_settings, _start_body

LABELLED = SAMPLE_GCODE + (
    "; start printing object, unique label id: 101\n"
    "G1 X20 Y30 F600\nG1 X40 Y50 E10\n"
    "; stop printing object, unique label id: 101\n"
)
PLATE_JSON = json.dumps(
    {"bbox_objects": [{"id": 101, "name": "bracket", "bbox": [18, 28, 42, 52]}]}
)


def _wait_for(check: Callable[[], object], timeout: float = 6.0) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _events(client: TestClient) -> list[dict]:
    return client.get("/events", params={"limit": 500}).json()["events"]


def _event(client: TestClient, kind: str, submission_id: str | None = None) -> dict:
    def find() -> dict | None:
        for event in _events(client):
            if event["type"] == kind and (
                submission_id is None or event["submission_id"] == submission_id
            ):
                return event
        return None

    return _wait_for(find)  # type: ignore[return-value]


def _upload(client: TestClient, tmp_path: Path, *, body: str = LABELLED, **form: str) -> dict:
    artifact = write_3mf(
        tmp_path / f"part-{time.monotonic_ns()}.3mf",
        plate_gcode=body,
        extra_members={"Metadata/plate_1.json": PLATE_JSON},
    )
    response = client.post(
        "/submissions",
        files={"file": ("part.3mf", artifact.read_bytes(), "application/octet-stream")},
        data={"target_machine": PRINTER, "requested_by": "submitter", **form},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def control(settings: Settings, backend: FakeBackend):
    backend.refresh_timestamps = True
    app = create_app(settings=_control_settings(settings), backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        yield client, backend


# -- result bundles and terminal events -----------------------------------------


def test_a_finished_print_leaves_a_final_bundle_and_a_finished_event(
    control, tmp_path: Path
) -> None:
    client, backend = control
    job = _upload(client, tmp_path)
    sid = job["submission_id"]
    client.post(f"/submissions/{sid}/approve", json={"approved_by": "approver"})
    headers = {"X-Claim-Token": _claim(client)}
    snap = client.post(f"{BASE}/control/snapshot", headers=headers).json()
    started = client.post(
        f"{BASE}/control/start_print",
        headers=headers,
        json=_start_body(sid, plate_check_method="printer_camera", snapshot_token=snap["token"]),
    )
    assert started.json()["state"] == "running", started.text
    backend.reading = replace(backend.reading, gcode_state="FINISH")

    finished = _event(client, "job.finished", sid)
    assert finished["schema_version"] == 1
    assert finished["printer_id"] == PRINTER
    assert finished["state"] == "finished"
    assert finished["gateway_id"] == "bambu_gateway"
    assert finished["updated_at"]
    assert finished["object_positions"][0]["name"] == "bracket"
    assert finished["object_positions"][0]["center_x_mm"] == 30.0
    assert finished["pickup"]["bed_temperature_c"] == 25.2
    assert finished["pickup"]["cooled"] is True
    assert _event(client, "job.started", sid)["object_positions"]

    bundle = _wait_for(lambda: client.get(f"/submissions/{sid}/result").json().get("final") and
                       client.get(f"/submissions/{sid}/result").json())
    assert bundle["schema_id"] == "ac.bambu.print_result"
    assert bundle["schema_version"] == 1
    assert bundle["state"] == "finished"
    assert bundle["artifact"]["sha256"] == job["sha256"]
    assert bundle["artifact"]["stored"] is True
    assert bundle["plate_check"]["snapshot_sha256"] == snap["sha256"]
    assert bundle["timeline"]["observed_running_at"]
    assert bundle["timeline"]["observed_finished_at"]
    assert bundle["objects"][0]["source"] == "slicer_plate_json"
    assert bundle["bed_size_mm"] == [256.0, 256.0]
    assert bundle["pickup"]["cooled"] is True
    assert bundle["job"]["history"][-1]["to_state"] == "finished"

    pickup = client.get(f"/submissions/{sid}/pickup-targets").json()
    assert pickup["ready"] is True
    assert pickup["objects"][0]["bounds"]["max_x_mm"] == 42.0
    assert "arm_frame" not in json.dumps(pickup)


def test_a_hot_finished_bed_announces_job_cooled_when_it_cools(
    control, tmp_path: Path
) -> None:
    client, backend = control
    sid = _upload(client, tmp_path)["submission_id"]
    client.post(f"/submissions/{sid}/approve", json={"approved_by": "approver"})
    headers = {"X-Claim-Token": _claim(client)}
    client.post(f"{BASE}/control/start_print", headers=headers, json=_start_body(sid))
    backend.reading = replace(backend.reading, gcode_state="FINISH", bed_temperature_c=60.0)

    finished = _event(client, "job.finished", sid)
    assert finished["pickup"]["cooled"] is False
    assert client.get(f"/submissions/{sid}/pickup-targets").json()["ready"] is False

    backend.reading = replace(backend.reading, bed_temperature_c=30.0)
    cooled = _event(client, "job.cooled", sid)
    assert cooled["pickup"]["cooled"] is True
    assert cooled["object_positions"][0]["name"] == "bracket"


def test_rejected_and_cancelled_jobs_get_bundles_events_and_local_only_records(
    client: TestClient, tmp_path: Path
) -> None:
    rejected = _upload(
        client, tmp_path, body=LABELLED.replace("nozzle_diameter = 0.4", "nozzle_diameter = 0.6")
    )
    cancelled = _upload(client, tmp_path)
    client.post(f"/submissions/{cancelled['submission_id']}/cancel", json={"cancelled_by": "op"})

    for kind, job in (("job.rejected", rejected), ("job.cancelled", cancelled)):
        event = _event(client, kind, job["submission_id"])
        assert event["state"] == kind.split(".")[1]
        bundle = _wait_for(
            lambda job=job: client.get(f"/submissions/{job['submission_id']}/result").json()
            if client.get(f"/submissions/{job['submission_id']}/result").json()["final"]
            else None
        )
        assert bundle["state"] == event["state"]
        entry = client.get(f"/submissions/{job['submission_id']}/eln-ingest").json()
        assert entry["state"] == "local_only"
        assert entry["local_reason"] == "no_eln_project"


def test_a_running_jobs_bundle_is_built_live_and_not_final(client: TestClient, tmp_path) -> None:
    sid = _upload(client, tmp_path)["submission_id"]
    bundle = client.get(f"/submissions/{sid}/result").json()
    assert bundle["final"] is False
    assert bundle["state"] == "queued"
    assert bundle["objects"][0]["name"] == "bracket"


def test_the_bundle_survives_deleting_the_job_record(client: TestClient, tmp_path) -> None:
    sid = _upload(client, tmp_path)["submission_id"]
    client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "op"})
    _wait_for(lambda: client.get(f"/submissions/{sid}/result").json()["final"])

    assert client.delete(f"/submissions/{sid}").status_code == 204
    assert client.get(f"/submissions/{sid}").status_code == 404
    assert client.get(f"/submissions/{sid}/result").json()["state"] == "cancelled"


# -- artifact download ------------------------------------------------------------


def test_the_exact_artifact_downloads_with_its_sha256_as_etag(
    client: TestClient, tmp_path: Path
) -> None:
    job = _upload(client, tmp_path)
    url = f"/submissions/{job['submission_id']}/artifact"

    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["etag"] == f'"{job["sha256"]}"'
    import hashlib

    assert hashlib.sha256(response.content).hexdigest() == job["sha256"]
    assert "attachment" in response.headers["content-disposition"]

    cached = client.get(url, headers={"If-None-Match": f'"{job["sha256"]}"'})
    assert cached.status_code == 304


def test_a_deleted_artifact_is_gone(settings: Settings, backend: FakeBackend, tmp_path) -> None:
    settings.submissions.cancel_deletes_artifact = True
    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        sid = _upload(client, tmp_path)["submission_id"]
        client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "op"})
        assert client.get(f"/submissions/{sid}/artifact").status_code == 410


# -- events across restarts ---------------------------------------------------------


def test_event_ids_keep_increasing_across_a_restart_with_a_marker(
    settings: Settings, backend: FakeBackend, tmp_path: Path
) -> None:
    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        sid = _upload(client, tmp_path)["submission_id"]
        client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "op"})
        before = _event(client, "job.cancelled", sid)
        first_boot = before["boot_id"]

    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        events = _events(client)
        assert before["event_id"] in [e["event_id"] for e in events]
        restart = [e for e in events if e["type"] == "gateway.restarted"][-1]
        assert restart["event_id"] > before["event_id"]
        assert restart["boot_id"] != first_boot
        after = client.get("/events", params={"after": before["event_id"]}).json()["events"]
        assert [e["type"] for e in after] == ["gateway.restarted"]


def test_a_dispatch_interrupted_by_a_restart_is_announced_as_failed(
    settings: Settings, backend: FakeBackend, tmp_path: Path
) -> None:
    from bambu_server.profiles import ObservedMachineState, build_profile
    from bambu_server.submissions import run_validation

    async def leave_a_job_dispatching() -> str:
        store = SubmissionStore(settings.submissions)
        store.load()

        async def chunks():
            yield SAMPLE_GCODE.encode()

        job = await store.accept(
            chunks=chunks(), extension=".gcode", target_machine=PRINTER,
            requested_by="submitter", material=None, original_filename="part.gcode",
        )
        profile = build_profile(settings.printers[0], ObservedMachineState(telemetry_ok=True))
        job = await run_validation(store, job, profile)
        job = await store.approve(job, approved_by="approver")
        now = datetime.now(UTC)
        record = DispatchRecord(
            dispatched_by="op", dispatched_by_verified=False, requested_at=now,
            plate_check=PlateCheck(
                confirmed_by="op", confirmed_by_verified=False, method="in_person",
                confirmed_at=now,
            ),
            ams_mapping=[TrayAssignment(filament_id=1, ams_id=0, tray_id=1)],
            bed_type="textured_plate", plate_index=1, remote_filename="gw_x.3mf",
        )
        await store.begin_dispatch(job, record)
        return job.submission_id

    sid = asyncio.run(leave_a_job_dispatching())
    app = create_app(settings=settings, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        event = _event(client, "job.failed", sid)
        assert event["recovered_after_restart"] is True
        assert "restarted while dispatching" in event["note"]
        bundle = _wait_for(lambda: client.get(f"/submissions/{sid}/result").json()["final"]
                           and client.get(f"/submissions/{sid}/result").json())
        assert bundle["state"] == "failed"


def test_event_and_outbox_failures_never_break_a_state_change(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    async def fail(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(OutboxStore, "ensure_for_terminal", fail)
    monkeypatch.setattr(ResultStore, "write", fail)
    sid = _upload(client, tmp_path)["submission_id"]

    response = client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "op"})

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert _event(client, "job.cancelled", sid)["state"] == "cancelled"


# -- listing changes ------------------------------------------------------------------


def test_submissions_can_be_paged_newest_first_since_a_time(
    client: TestClient, tmp_path: Path
) -> None:
    first = _upload(client, tmp_path)
    second = _upload(client, tmp_path)
    client.post(f"/submissions/{first['submission_id']}/approve", json={"approved_by": "op"})

    newest = client.get("/submissions", params={"order": "newest", "limit": 1}).json()
    assert [j["submission_id"] for j in newest] == [first["submission_id"]]
    page_two = client.get(
        "/submissions", params={"order": "newest", "limit": 1, "offset": 1}
    ).json()
    assert [j["submission_id"] for j in page_two] == [second["submission_id"]]
    changed = client.get(
        "/submissions", params={"updated_after": second["updated_at"]}
    ).json()
    assert [j["submission_id"] for j in changed] == [first["submission_id"]]
    # The default order is unchanged: oldest submission first.
    default = client.get("/submissions").json()
    assert [j["submission_id"] for j in default][:2] == [
        first["submission_id"], second["submission_id"],
    ]


# -- the outbox worker against a fake central server ----------------------------------


class FakeCentral:
    def __init__(self, *, post: int = 202, polls: tuple[tuple[int, str], ...] = ()) -> None:
        self.post_status = post
        self.polls = list(polls)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        if method == "POST":
            return self.post_status, b'{"state": "delivered"}'
        status, state = self.polls.pop(0) if self.polls else (200, "filed")
        return status, json.dumps(
            {"state": state, "eln_entry_url": "https://eln.example/entry/7"}
        ).encode()


def _worker(tmp_path: Path, central: FakeCentral) -> tuple[OutboxWorker, OutboxStore]:
    outbox = OutboxStore(tmp_path)
    outbox.load()
    results = ResultStore(tmp_path)
    results.root.mkdir(parents=True, exist_ok=True)
    results.path("a" * 32).write_text('{"schema_version": 1, "state": "finished"}')
    worker = OutboxWorker(
        outbox=outbox, results=results, url="https://central.example/api/ingest/print-results",
        token="test-token", gateway_id="bambu_gateway", poll_interval_s=60,
        retry_initial_s=10, retry_max_s=40, request_timeout_s=5, transport=central,
    )
    return worker, outbox


async def _pending(outbox: OutboxStore) -> None:
    now = datetime.now(UTC)
    await outbox.put(OutboxEntry(
        submission_id="a" * 32, device_id="bambu_a1_01", state="pending",
        eln_project="PRJ-1", created_at=now, updated_at=now, next_attempt_at=now,
    ))


LATER = datetime.now(UTC) + timedelta(days=1)


async def test_the_worker_delivers_then_follows_filing_to_filed(tmp_path: Path) -> None:
    central = FakeCentral(polls=((200, "filing"), (200, "filed")))
    worker, outbox = _worker(tmp_path, central)
    await _pending(outbox)

    await worker.tick(LATER)
    assert outbox.get("a" * 32).state == "delivered"
    method, url, headers, body = central.calls[0]
    assert method == "POST" and url.endswith("/api/ingest/print-results")
    assert headers["Authorization"] == "Bearer test-token"
    payload = json.loads(body)
    assert payload["device_id"] == "bambu_a1_01"
    assert payload["id"] == "a" * 32
    assert payload["eln_project"] == "PRJ-1"
    assert payload["bundle"]["state"] == "finished"

    await worker.tick(LATER)
    assert outbox.get("a" * 32).state == "filing"
    assert central.calls[1][1].endswith(f"/bambu_a1_01/{'a' * 32}")
    await worker.tick(LATER)
    entry = outbox.get("a" * 32)
    assert entry.state == "filed"
    assert entry.remote_reference == "https://eln.example/entry/7"
    assert entry.next_attempt_at is None


async def test_the_worker_backs_off_exponentially_on_server_errors(tmp_path: Path) -> None:
    worker, outbox = _worker(tmp_path, FakeCentral(post=503))
    await _pending(outbox)

    await worker.tick(LATER)
    first = outbox.get("a" * 32)
    assert first.state == "pending" and first.attempts == 1
    assert 9 <= (first.next_attempt_at - first.updated_at).total_seconds() <= 11
    await worker.tick(LATER)
    second = outbox.get("a" * 32)
    assert second.attempts == 2
    assert 19 <= (second.next_attempt_at - second.updated_at).total_seconds() <= 21
    assert "503" in second.last_error


async def test_a_refused_bundle_is_held_for_a_person(tmp_path: Path) -> None:
    worker, outbox = _worker(tmp_path, FakeCentral(post=422))
    await _pending(outbox)
    await worker.tick(LATER)
    entry = outbox.get("a" * 32)
    assert entry.state == "held"
    assert entry.next_attempt_at is None


async def test_an_unknown_delivery_is_sent_again(tmp_path: Path) -> None:
    worker, outbox = _worker(tmp_path, FakeCentral(polls=((404, ""),)))
    await _pending(outbox)
    await worker.tick(LATER)
    await worker.tick(LATER)
    assert outbox.get("a" * 32).state == "pending"


async def test_an_unreachable_server_is_retried(tmp_path: Path) -> None:
    def down(*_args):
        raise ConnectionRefusedError()

    worker, outbox = _worker(tmp_path, down)  # type: ignore[arg-type]
    await _pending(outbox)
    await worker.tick(LATER)
    entry = outbox.get("a" * 32)
    assert entry.state == "pending" and entry.attempts == 1
    assert "unreachable" in entry.last_error


async def test_outbox_state_survives_a_restart(tmp_path: Path) -> None:
    worker, outbox = _worker(tmp_path, FakeCentral())
    await _pending(outbox)
    await worker.tick(LATER)

    reopened = OutboxStore(tmp_path)
    reopened.load()
    assert reopened.get("a" * 32).state == "delivered"
    assert reopened.counts()["delivered"] == 1


# -- ingest through the API -----------------------------------------------------------


@pytest.fixture
def ingest(settings: Settings, backend: FakeBackend, monkeypatch):
    monkeypatch.setenv("BAMBU_RESULTS_TOKEN", "test-token")
    settings.results.ingest_url = "https://central.example/api/ingest/print-results"
    settings.results.worker_tick_s = 0.05
    settings.results.poll_interval_s = 0.05
    central = FakeCentral(polls=((200, "filing"), (200, "filed")))
    app = create_app(
        settings=settings, backend_factory=lambda _d, _c: backend, results_transport=central
    )
    with TestClient(app) as client:
        yield client, central


def test_a_job_with_an_eln_project_is_filed_through_the_central_server(
    ingest, tmp_path: Path
) -> None:
    client, central = ingest
    assert client.get("/status").json()["details"]["results_ingest_configured"] is True
    sid = _upload(client, tmp_path, eln_project="PRJ-42")["submission_id"]
    client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "op"})

    entry = _wait_for(
        lambda: (e := client.get(f"/submissions/{sid}/eln-ingest").json()).get("state")
        == "filed" and e
    )
    assert entry["eln_project"] == "PRJ-42"
    posted = json.loads(next(c[3] for c in central.calls if c[0] == "POST"))
    assert posted["bundle"]["submission_id"] == sid
    assert posted["bundle"]["eln_project"] == "PRJ-42"
    assert client.get("/status").json()["details"]["results_outbox"]["filed"] == 1


def test_a_finished_job_can_be_filed_later_under_a_chosen_project(
    ingest, tmp_path: Path
) -> None:
    client, _central = ingest
    sid = _upload(client, tmp_path)["submission_id"]
    queued = client.post(f"/submissions/{sid}/eln-ingest", json={"eln_project": "PRJ-7",
                                                                  "requested_by": "op"})
    assert queued.status_code == 409  # not terminal yet

    client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "op"})
    _wait_for(lambda: client.get(f"/submissions/{sid}/eln-ingest").status_code == 200)
    assert client.get(f"/submissions/{sid}/eln-ingest").json()["state"] == "local_only"

    queued = client.post(
        f"/submissions/{sid}/eln-ingest", json={"eln_project": "PRJ-7", "requested_by": "op"}
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["queued_by"] == "op"
    _wait_for(
        lambda: client.get(f"/submissions/{sid}/eln-ingest").json().get("state") == "filed"
    )
    again = client.post(
        f"/submissions/{sid}/eln-ingest", json={"eln_project": "PRJ-7", "requested_by": "op"}
    )
    assert again.status_code == 409


def test_without_central_ingest_records_stay_local_only(client: TestClient, tmp_path) -> None:
    sid = _upload(client, tmp_path, eln_project="PRJ-1")["submission_id"]
    client.post(f"/submissions/{sid}/cancel", json={"cancelled_by": "op"})
    entry = _wait_for(
        lambda: client.get(f"/submissions/{sid}/eln-ingest").status_code == 200
        and client.get(f"/submissions/{sid}/eln-ingest").json()
    )
    assert entry["state"] == "local_only"
    assert entry["local_reason"] == "ingest_not_configured"
    refused = client.post(
        f"/submissions/{sid}/eln-ingest", json={"eln_project": "PRJ-1", "requested_by": "op"}
    )
    assert refused.status_code == 409
    assert "not configured" in refused.json()["detail"]
