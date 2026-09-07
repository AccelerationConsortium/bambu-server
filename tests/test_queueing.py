"""Per-machine queue view and expected finish times."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bambu_server.models import EquipmentStatus, MetricValue
from bambu_server.queueing import build_queue_view
from bambu_server.submissions import StateTransition, SubmissionJob

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _status(
    *, activity: str = "idle", remaining: float | None = None, job_name: str | None = None
) -> EquipmentStatus:
    metrics = {}
    if remaining is not None:
        metrics["remaining_time"] = MetricValue(value=remaining, unit="min")
        metrics["print_progress"] = MetricValue(value=42, unit="%")
    return EquipmentStatus(
        equipment_id="bambu_test_01",
        equipment_name="Bambu Test 01",
        equipment_kind="other",
        equipment_status="busy" if activity == "running" else "ready",
        activity=activity,
        device_time=NOW,
        metrics=metrics,
        details={"job_name": job_name} if job_name else {},
    )


def _job(name: str, duration: float | None, *, state: str = "queued") -> SubmissionJob:
    return SubmissionJob(
        submission_id=name.ljust(32, "0")[:32],
        target_machine="bambu_test_01",
        requested_by="remote-user-1",
        original_filename=f"{name}.gcode",
        artifact_kind="gcode",
        byte_size=1,
        sha256="0" * 64,
        state=state,
        created_at=NOW,
        updated_at=NOW,
        estimated_duration_minutes=duration,
        approved_at=NOW if state == "approved" else None,
        history=[StateTransition(at=NOW, to_state=state)],
    )


def test_an_idle_machine_stacks_queued_jobs_from_now() -> None:
    view = build_queue_view(
        machine="bambu_test_01",
        status=_status(),
        jobs=[_job("a", 30.0), _job("b", 15.0)],
        now=NOW,
    )

    assert view.running is None
    assert view.estimates_complete is True
    assert [job.position for job in view.queued] == [1, 2]
    assert view.queued[0].expected_end == NOW + timedelta(minutes=30)
    assert view.queued[1].expected_end == NOW + timedelta(minutes=45)


def test_a_running_job_pushes_the_queue_out_by_its_remaining_time() -> None:
    view = build_queue_view(
        machine="bambu_test_01",
        status=_status(activity="running", remaining=20.0, job_name="live.3mf"),
        jobs=[_job("a", 30.0)],
        now=NOW,
    )

    assert view.running is not None
    assert view.running.job_name == "live.3mf"
    assert view.running.progress_percent == 42.0
    assert view.running.expected_end == NOW + timedelta(minutes=20)
    assert view.queued[0].expected_end == NOW + timedelta(minutes=50)
    assert view.estimates_complete is True


def test_an_unknown_remaining_time_makes_every_estimate_null() -> None:
    """A printer that will not say how long it has left cannot found an ETA."""
    view = build_queue_view(
        machine="bambu_test_01",
        status=_status(activity="running", job_name="live.3mf"),
        jobs=[_job("a", 30.0)],
        now=NOW,
    )

    assert view.running.expected_end is None
    assert view.queued[0].expected_end is None
    assert view.estimates_complete is False


def test_one_job_without_a_duration_truncates_the_rest() -> None:
    view = build_queue_view(
        machine="bambu_test_01",
        status=_status(),
        jobs=[_job("a", 30.0), _job("b", None), _job("c", 10.0)],
        now=NOW,
    )

    assert view.queued[0].expected_end == NOW + timedelta(minutes=30)
    assert view.queued[1].expected_end is None
    # Everything behind an unknown duration is unknowable too.
    assert view.queued[2].expected_end is None
    assert view.estimates_complete is False


def test_approval_is_visible_in_the_queue() -> None:
    view = build_queue_view(
        machine="bambu_test_01",
        status=_status(),
        jobs=[_job("a", 10.0, state="approved")],
        now=NOW,
    )

    assert view.queued[0].state == "approved"
    assert view.queued[0].approved is True


def test_an_unknown_activity_reports_no_running_job() -> None:
    """Unreachable or stale telemetry is not evidence that a job is running."""
    view = build_queue_view(
        machine="bambu_test_01", status=_status(activity="unknown"), jobs=[], now=NOW
    )

    assert view.activity == "unknown"
    assert view.running is None
