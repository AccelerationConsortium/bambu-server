"""Per-machine queue view and expected finish times.

The queue is gateway-owned, which is what makes the estimate honest: the
running job's remaining time comes from the printer's own telemetry, and each
waiting job's duration comes from the slicer's estimate embedded in the
submitted artifact. Nothing here guesses.

Building this view reads cached telemetry and the submission store only. It
performs no printer I/O and mutates no job, so a dashboard may poll it freely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from .models import Activity, EquipmentStatus
from .submissions import JobState, SubmissionJob


class RunningJobView(BaseModel):
    """The job the printer says it is running.

    It is not correlated with a submission: this service never dispatches, so a
    running print was started by some other route to the printer (Bambu Studio,
    the handset, the cloud) and the gateway can only report what it observes.
    """

    job_name: str | None = None
    progress_percent: float | None = None
    remaining_time_minutes: float | None = None
    expected_end: datetime | None = None


class QueuedJobView(BaseModel):
    submission_id: str
    position: int
    state: JobState
    job_name: str
    requested_by: str
    material: str | None = None
    approved: bool = False
    estimated_duration_minutes: float | None = None
    expected_end: datetime | None = None


class QueueView(BaseModel):
    machine: str
    generated_at: datetime
    activity: Activity
    running: RunningJobView | None = None
    queued: list[QueuedJobView] = Field(default_factory=list)
    #: False when any expected finish time could not be computed -- an unknown
    #: remaining time on the running job, or a queued job whose artifact carries
    #: no duration estimate. Everything after the first unknown is ``null``.
    estimates_complete: bool = True


def _metric(status: EquipmentStatus, name: str) -> float | None:
    metric = status.metrics.get(name)
    if metric is None:
        return None
    value = metric.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def build_queue_view(
    *,
    machine: str,
    status: EquipmentStatus,
    jobs: list[SubmissionJob],
    now: datetime | None = None,
) -> QueueView:
    now = now or datetime.now(UTC)

    running: RunningJobView | None = None
    # `cursor` is the instant the printer becomes free. It stays None while
    # that instant is unknowable, which makes every downstream estimate null
    # rather than wrong.
    cursor: datetime | None = now
    estimates_complete = True

    if status.activity == "running":
        remaining = _metric(status, "remaining_time")
        job_name = status.details.get("job_name")
        expected_end = now + timedelta(minutes=remaining) if remaining is not None else None
        running = RunningJobView(
            job_name=job_name if isinstance(job_name, str) else None,
            progress_percent=_metric(status, "print_progress"),
            remaining_time_minutes=remaining,
            expected_end=expected_end,
        )
        cursor = expected_end
        if expected_end is None:
            estimates_complete = False

    queued: list[QueuedJobView] = []
    for position, job in enumerate(jobs, start=1):
        duration = job.estimated_duration_minutes
        if cursor is not None and duration is not None:
            cursor = cursor + timedelta(minutes=duration)
            expected_end = cursor
        else:
            expected_end = None
            cursor = None
            estimates_complete = False
        queued.append(
            QueuedJobView(
                submission_id=job.submission_id,
                position=position,
                state=job.state,
                job_name=job.original_filename,
                requested_by=job.requested_by,
                material=job.material,
                approved=job.approved_at is not None,
                estimated_duration_minutes=duration,
                expected_end=expected_end,
            )
        )

    return QueueView(
        machine=machine,
        generated_at=now,
        activity=status.activity,
        running=running,
        queued=queued,
        estimates_complete=estimates_complete,
    )
