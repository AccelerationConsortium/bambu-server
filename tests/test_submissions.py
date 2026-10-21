"""Submission store, state machine, and dispatch bookkeeping."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bambu_server.config import MachineProfileConfig, PrinterDefinition, SubmissionSettings
from bambu_server.profiles import MachineProfile, ObservedMachineState, build_profile
from bambu_server.submissions import (
    ArtifactTooLarge,
    DispatchRecord,
    InvalidTransition,
    PlateCheck,
    SubmissionError,
    SubmissionStore,
    TrayAssignment,
    run_validation,
    safe_display_name,
)

from .conftest import SAMPLE_GCODE, write_3mf, write_gcode


@pytest.fixture
def store(tmp_path: Path) -> SubmissionStore:
    store = SubmissionStore(SubmissionSettings(directory=tmp_path / "submissions"))
    store.load()
    return store


@pytest.fixture
def profile() -> MachineProfile:
    definition = PrinterDefinition(
        id="bambu_test_01",
        name="Bambu Test 01",
        model="X1 Carbon",
        env_prefix="BAMBU_TEST_01",
        profile=MachineProfileConfig.model_validate(
            {
                "nozzle_diameter_mm": 0.4,
                "bed_size_mm": [256, 256],
                "limits": {"nozzle_temperature_c": [0, 300], "bed_temperature_c": [0, 110]},
            }
        ),
    )
    return build_profile(definition, ObservedMachineState(telemetry_ok=True))


async def _chunks(data: bytes, size: int = 4096) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


async def _accept(store: SubmissionStore, body: bytes = None, extension: str = ".gcode"):
    payload = SAMPLE_GCODE.encode() if body is None else body
    return await store.accept(
        chunks=_chunks(payload),
        extension=extension,
        target_machine="bambu_test_01",
        requested_by="remote-user-1",
        material=None,
        original_filename="../../etc/passwd.gcode",
    )


async def test_accept_stores_the_artifact_and_records_the_job(
    store: SubmissionStore,
) -> None:
    job = await _accept(store)

    assert job.state == "submitted"
    assert job.artifact_kind == "gcode"
    assert job.byte_size == len(SAMPLE_GCODE.encode())
    assert len(job.sha256) == 64
    # The stored name comes from the submission's UUID, never the client's path.
    assert store.artifact_path(job).name == f"{job.submission_id}.gcode"
    assert store.artifact_path(job).read_text() == SAMPLE_GCODE
    assert job.original_filename == "passwd.gcode"


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\\\Windows\\\\evil.gcode", "evil.gcode"),
        ("", "submission"),
        ("na\x00me.gcode", "name.gcode"),
    ],
)
def test_display_names_are_reduced_to_a_basename(supplied: str, expected: str) -> None:
    assert safe_display_name(supplied) == expected


async def test_jobs_survive_a_restart(store: SubmissionStore, tmp_path: Path) -> None:
    job = await _accept(store)

    reopened = SubmissionStore(SubmissionSettings(directory=tmp_path / "submissions"))
    reopened.load()

    assert reopened.get(job.submission_id) is not None
    assert reopened.get(job.submission_id).state == "submitted"


async def test_an_oversized_upload_is_refused_and_leaves_nothing_behind(
    tmp_path: Path,
) -> None:
    store = SubmissionStore(
        SubmissionSettings(directory=tmp_path / "submissions", max_file_bytes=1024)
    )
    store.load()

    with pytest.raises(ArtifactTooLarge):
        await _accept(store, body=b"x" * 4096)

    assert list((tmp_path / "submissions").iterdir()) == []
    assert store.list() == []


async def test_an_empty_upload_is_refused(store: SubmissionStore) -> None:
    with pytest.raises(SubmissionError):
        await _accept(store, body=b"")


async def test_illegal_transitions_are_refused(store: SubmissionStore) -> None:
    job = await _accept(store)

    with pytest.raises(InvalidTransition):
        await store.transition(job, "running")

    job = await store.transition(job, "validating")
    job = await store.transition(job, "rejected")
    # Rejected is terminal: nothing moves out of it, not even to `failed`.
    with pytest.raises(InvalidTransition):
        await store.transition(job, "failed")


async def test_validation_queues_a_conforming_submission(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    job = await run_validation(store, await _accept(store), profile)

    assert job.state == "queued"
    assert job.verdict is not None
    assert job.verdict.verdict == "pass"
    assert job.verdict.dispatch_ready is False
    assert job.estimated_duration_minutes == 70.0
    assert [entry.to_state for entry in job.history] == [
        "submitted",
        "validating",
        "validated",
        "queued",
    ]


async def test_validation_rejects_a_nonconforming_submission(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    job = await run_validation(store, await _accept(store, body.encode()), profile)

    assert job.state == "rejected"
    assert job.verdict.reasons == ["machine_compatible"]
    assert store.queue_for("bambu_test_01") == []


async def test_an_unreadable_artifact_is_rejected_without_leaking_a_path(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    job = await _accept(store, body=b"PK\x03\x04 not really a zip", extension=".3mf")
    job = await run_validation(store, job, profile)

    assert job.state == "rejected"
    assert job.verdict.reasons == ["artifact_readable"]
    detail = job.verdict.checks[0].detail
    assert "could not be opened" in detail
    # The reason must never quote where the gateway stored the file.
    assert str(store.root) not in detail


async def test_a_3mf_submission_validates_end_to_end(
    store: SubmissionStore, profile: MachineProfile, tmp_path: Path
) -> None:
    payload = write_3mf(tmp_path / "plate.3mf").read_bytes()
    job = await run_validation(
        store, await _accept(store, payload, extension=".3mf"), profile
    )

    assert job.state == "queued"
    assert job.facts.kind == "3mf"
    assert job.facts.sliced is True


async def test_approval_requires_a_queued_job_that_passed(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    submitted = await _accept(store)
    with pytest.raises(InvalidTransition):
        await store.approve(submitted, approved_by="operator")

    job = await run_validation(store, submitted, profile)
    approved = await store.approve(job, approved_by="operator")

    assert approved.state == "approved"
    assert approved.approved_by == "operator"
    assert approved.approved_at is not None
    # dispatch_ready flips only once validation *and* the approval gate agree.
    assert approved.verdict.dispatch_ready is True

    with pytest.raises(InvalidTransition):
        await store.approve(approved, approved_by="operator")


async def test_a_rejected_job_can_never_be_approved(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    job = await run_validation(store, await _accept(store, body.encode()), profile)

    with pytest.raises(InvalidTransition):
        await store.approve(job, approved_by="operator")


async def test_the_queue_is_first_in_first_out_and_holds_approved_jobs(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    first = await run_validation(store, await _accept(store), profile)
    second = await run_validation(store, await _accept(store), profile)
    await store.approve(first, approved_by="operator")

    queued = store.queue_for("bambu_test_01")
    assert [job.submission_id for job in queued] == [
        first.submission_id,
        second.submission_id,
    ]
    assert queued[0].state == "approved"
    assert queued[1].state == "queued"


async def test_the_queue_is_scoped_to_one_machine(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    await run_validation(store, await _accept(store), profile)
    assert store.queue_for("bambu_other") == []


def _record() -> DispatchRecord:
    now = datetime.now(UTC)
    return DispatchRecord(
        dispatched_by="operator",
        dispatched_by_verified=True,
        requested_at=now,
        plate_check=PlateCheck(
            confirmed_by="operator", confirmed_by_verified=True, method="in_person",
            confirmed_at=now,
        ),
        ams_mapping=[TrayAssignment(filament_id=1, ams_id=0, tray_id=1)],
        bed_type="textured_plate",
        plate_index=1,
        remote_filename="gw_x.3mf",
    )


async def test_dispatch_requires_approval_and_happens_once(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    """Only an approved job can enter dispatching, and only one attempt is recorded."""
    job = await run_validation(store, await _accept(store), profile)
    with pytest.raises(InvalidTransition):
        await store.begin_dispatch(job, _record())

    approved = await store.approve(job, approved_by="operator")
    dispatching = await store.begin_dispatch(approved, _record())
    assert dispatching.state == "dispatching"
    assert dispatching.dispatch is not None

    failed = await store.update_dispatch(dispatching, to_state="failed", note="test")
    with pytest.raises(InvalidTransition):
        await store.begin_dispatch(failed, _record())


async def test_a_restart_fails_an_interrupted_dispatch_with_its_uncertainty(
    store: SubmissionStore, profile: MachineProfile, tmp_path: Path
) -> None:
    job = await run_validation(store, await _accept(store), profile)
    job = await store.begin_dispatch(await store.approve(job, approved_by="op"), _record())

    reloaded = SubmissionStore(SubmissionSettings(directory=tmp_path / "submissions"))
    reloaded.load()
    after = reloaded.get(job.submission_id)

    assert after is not None and after.state == "failed"
    assert after.dispatch is not None
    assert "may or may not" in (after.dispatch.uncertainty or "")


async def test_a_corrupt_record_does_not_stop_the_store_loading(
    store: SubmissionStore, tmp_path: Path
) -> None:
    job = await _accept(store)
    (tmp_path / "submissions" / "broken.json").write_text("{not json", encoding="utf-8")

    reopened = SubmissionStore(SubmissionSettings(directory=tmp_path / "submissions"))
    reopened.load()

    assert [item.submission_id for item in reopened.list()] == [job.submission_id]


def test_writing_a_gcode_helper_is_deterministic(tmp_path: Path) -> None:
    assert write_gcode(tmp_path / "a.gcode").read_text() == SAMPLE_GCODE


async def test_a_blank_owner_is_refused_before_anything_is_written(
    store: SubmissionStore, tmp_path: Path
) -> None:
    """Refusing after the write would leave an artifact no record points at."""
    with pytest.raises(SubmissionError, match="requested_by"):
        await store.accept(
            chunks=_chunks(SAMPLE_GCODE.encode()),
            extension=".gcode",
            target_machine="bambu_test_01",
            requested_by="   ",
            material=None,
            original_filename="part.gcode",
        )

    assert list((tmp_path / "submissions").iterdir()) == []
    assert store.list() == []


async def test_a_blank_material_is_treated_as_absent(store: SubmissionStore) -> None:
    job = await store.accept(
        chunks=_chunks(SAMPLE_GCODE.encode()),
        extension=".gcode",
        target_machine="bambu_test_01",
        requested_by="remote-user-1",
        material="  ",
        original_filename="part.gcode",
    )
    assert job.material is None


async def test_cancelling_a_queued_job_removes_it_and_its_artifact(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    job = await run_validation(store, await _accept(store), profile)
    path = store.artifact_path(job)
    assert path.exists()

    cancelled = await store.cancel(job, cancelled_by="operator", reason="wrong plate")

    assert cancelled.state == "cancelled"
    assert cancelled.artifact_removed is True
    assert not path.exists()
    assert store.queue_for("bambu_test_01") == []
    # The record outlives the file, so the withdrawal stays explicable.
    assert cancelled.history[-1].note == "cancelled by operator: wrong plate"
    assert store.get(job.submission_id) is not None


async def test_cancelling_an_approved_job_retracts_dispatch_ready(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    job = await run_validation(store, await _accept(store), profile)
    approved = await store.approve(job, approved_by="operator")
    assert approved.verdict.dispatch_ready is True

    cancelled = await store.cancel(approved, cancelled_by="operator")

    assert cancelled.state == "cancelled"
    # A withdrawn job must not still read as cleared to run.
    assert cancelled.verdict.dispatch_ready is False


async def test_cancellation_is_terminal_and_not_repeatable(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    job = await run_validation(store, await _accept(store), profile)
    cancelled = await store.cancel(job, cancelled_by="operator")

    for attempt in (
        store.cancel(cancelled, cancelled_by="operator"),
        store.approve(cancelled, approved_by="operator"),
    ):
        with pytest.raises(InvalidTransition):
            await attempt


async def test_a_rejected_job_cannot_be_cancelled(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    """Rejection is already terminal; cancelling it would muddy the record."""
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    job = await run_validation(store, await _accept(store, body.encode()), profile)

    with pytest.raises(InvalidTransition, match="only a waiting submission"):
        await store.cancel(job, cancelled_by="operator")


async def test_cancel_refuses_a_blank_actor(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    job = await run_validation(store, await _accept(store), profile)

    with pytest.raises(SubmissionError, match="cancelled_by"):
        await store.cancel(job, cancelled_by="  ")

    assert store.get(job.submission_id).state == "queued"


def test_cancellation_can_never_reach_a_dispatched_job() -> None:
    """Cancel is a queue operation; stopping a print is the control plane's job."""
    from bambu_server.submissions import ALLOWED_TRANSITIONS, CANCELLABLE_STATES

    assert CANCELLABLE_STATES == {"queued", "approved"}
    for state in ("dispatching", "running", "finished", "failed", "rejected"):
        assert "cancelled" not in ALLOWED_TRANSITIONS[state]


# --- retention ---------------------------------------------------------------


def test_terminal_states_are_derived_from_the_transition_table() -> None:
    """A state added to the table cannot be forgotten in the retention set."""
    from bambu_server.submissions import ALLOWED_TRANSITIONS, TERMINAL_STATES

    assert TERMINAL_STATES == {"rejected", "finished", "failed", "cancelled"}
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
    for state in set(ALLOWED_TRANSITIONS) - TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state], state


async def test_only_a_finished_job_can_be_deleted(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    job = await run_validation(store, await _accept(store), profile)

    with pytest.raises(InvalidTransition, match="only a finished submission"):
        await store.forget(job)

    cancelled = await store.cancel(job, cancelled_by="operator")
    forgotten = await store.forget(cancelled)

    assert forgotten.submission_id == job.submission_id
    assert store.get(job.submission_id) is None
    assert not (store.root / f"{job.submission_id}.json").exists()


async def test_deleting_removes_a_leftover_artifact_too(
    store: SubmissionStore, profile: MachineProfile
) -> None:
    """A rejected job keeps its artifact; deleting the record must not orphan it."""
    body = SAMPLE_GCODE.replace("; nozzle_diameter = 0.4", "; nozzle_diameter = 0.6")
    job = await run_validation(store, await _accept(store, body.encode()), profile)
    path = store.artifact_path(job)
    assert job.state == "rejected"
    assert path.exists()

    await store.forget(job)

    assert not path.exists()
    assert list(store.root.iterdir()) == []


async def test_expired_terminal_records_are_swept_at_startup(
    store: SubmissionStore, profile: MachineProfile, tmp_path: Path
) -> None:
    job = await run_validation(store, await _accept(store), profile)
    cancelled = await store.cancel(job, cancelled_by="operator")

    # Backdate the record past the window, as an old job on disk would be.
    stale = cancelled.model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(days=40)}
    )
    (store.root / f"{job.submission_id}.json").write_text(
        stale.model_dump_json(), encoding="utf-8"
    )

    reopened = SubmissionStore(
        SubmissionSettings(directory=tmp_path / "submissions", retain_terminal_days=30)
    )
    reopened.load()

    assert reopened.get(job.submission_id) is None
    assert list((tmp_path / "submissions").iterdir()) == []


async def test_a_job_still_in_play_is_never_swept_however_old(
    store: SubmissionStore, profile: MachineProfile, tmp_path: Path
) -> None:
    """A job stuck mid-pipeline is a signal, not litter."""
    job = await run_validation(store, await _accept(store), profile)
    stale = job.model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(days=400)}
    )
    (store.root / f"{job.submission_id}.json").write_text(
        stale.model_dump_json(), encoding="utf-8"
    )

    reopened = SubmissionStore(
        SubmissionSettings(directory=tmp_path / "submissions", retain_terminal_days=1)
    )
    reopened.load()

    assert reopened.get(job.submission_id) is not None
    assert reopened.get(job.submission_id).state == "queued"


async def test_retention_can_be_disabled(
    store: SubmissionStore, profile: MachineProfile, tmp_path: Path
) -> None:
    job = await run_validation(store, await _accept(store), profile)
    cancelled = await store.cancel(job, cancelled_by="operator")
    stale = cancelled.model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(days=4000)}
    )
    (store.root / f"{job.submission_id}.json").write_text(
        stale.model_dump_json(), encoding="utf-8"
    )

    reopened = SubmissionStore(
        SubmissionSettings(
            directory=tmp_path / "submissions", retain_terminal_days=None
        )
    )
    reopened.load()

    assert reopened.get(job.submission_id) is not None
