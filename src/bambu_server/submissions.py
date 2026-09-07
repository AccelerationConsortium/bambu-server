"""Submission intake, job state machine, and durable job store.

A *submission* is a print artifact plus the metadata naming the machine it is
destined for. This module owns its whole life up to -- and deliberately not
including -- dispatch:

``submitted -> validating -> validated -> queued -> approved``

with ``rejected`` as the terminal outcome of a failed validation and ``failed``
reachable from anything that is not already rejected. The three states past
approval (``dispatching``, ``running``, ``finished``) are declared because the
contract declares them, but **nothing in this service can enter them**: dispatch
is the one printer-touching step and it stays behind the approval gate until the
control-plane design is approved. See :func:`dispatch`.

Nothing in this module performs printer I/O. Files land on the gateway host
under a configured directory; their paths are internal and never leave the
process, because a stored path is not something a client has any use for.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, Field, field_validator

from .artifacts import (
    ARTIFACT_EXTENSIONS,
    ArtifactError,
    ArtifactFacts,
    ArtifactKind,
    inspect_artifact,
)
from .config import SubmissionSettings
from .profiles import MachineProfile
from .validation import CheckResult, ValidationVerdict, validate_model

logger = logging.getLogger(__name__)

JobState = Literal[
    "submitted",
    "validating",
    "validated",
    "queued",
    "approved",
    "dispatching",
    "running",
    "finished",
    "failed",
    "rejected",
    "cancelled",
]

#: Which states are waiting in a machine's queue. ``approved`` stays queued
#: because approval alone moves nothing -- only dispatch does.
QUEUED_STATES: frozenset[str] = frozenset({"queued", "approved"})

#: States a submitter or operator may withdraw a job from. Deliberately only
#: the waiting ones: cancelling means "take this out of the queue", never "stop
#: a print". A job that has reached the printer is the control plane's problem,
#: and abort belongs there under a claim -- not on this surface.
CANCELLABLE_STATES: frozenset[str] = frozenset({"queued", "approved"})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "submitted": frozenset({"validating", "failed"}),
    "validating": frozenset({"validated", "rejected", "failed"}),
    "validated": frozenset({"queued", "failed"}),
    "queued": frozenset({"approved", "cancelled", "failed"}),
    "approved": frozenset({"dispatching", "cancelled", "failed"}),
    "dispatching": frozenset({"running", "failed"}),
    "running": frozenset({"finished", "failed"}),
    # Terminal. `failed` is deliberately terminal too: the contract sends it to
    # human reconciliation, and an automated drain back into the queue is
    # exactly the silent-recovery behaviour the lab rules forbid.
    "finished": frozenset(),
    "failed": frozenset(),
    "rejected": frozenset(),
    "cancelled": frozenset(),
}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SubmissionError(ValueError):
    """The submission cannot be accepted as presented."""


class ArtifactTooLarge(SubmissionError):
    """The upload exceeded the configured size ceiling."""


class InvalidTransition(RuntimeError):
    """A state change the job's state machine does not permit."""


class DispatchUnavailable(RuntimeError):
    """Dispatch is not implemented in this service."""


def clean_text(value: str | None, *, field: str, required: bool) -> str | None:
    """Normalise a client-supplied text field, or refuse it.

    Called before the upload is written, so a field that cannot be accepted
    fails the request instead of leaving an artifact on disk with no record
    pointing at it.
    """

    if value is None:
        if required:
            raise SubmissionError(f"{field} is required")
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()
    if not cleaned:
        if required:
            raise SubmissionError(f"{field} must not be blank")
        return None
    return cleaned


def _verified_suffix(verified: bool) -> str:
    """Mark a verified actor in free-text history.

    Structured fields carry a boolean; the history note is read by people, and
    "who did this, and did we actually check" is the part worth spelling out.
    """

    return " (verified identity)" if verified else ""


def safe_display_name(name: str) -> str:
    """Reduce a client-supplied filename to something safe to echo back.

    Only the basename survives, control characters are stripped, and the result
    is capped. It is display metadata: it is never joined to a path, and the
    stored artifact is named from the submission's UUID instead.
    """

    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = _CONTROL_CHARS.sub("", base)
    return base[:200] or "submission"


class StateTransition(BaseModel):
    at: datetime
    from_state: JobState | None = None
    to_state: JobState
    note: str | None = None


class SubmissionJob(BaseModel):
    """One submitted print job. Safe to return to a client verbatim."""

    submission_id: str
    target_machine: str
    requested_by: str = Field(min_length=1, max_length=120)
    #: True when `requested_by` is an edge-verified identity rather than a name
    #: the client typed. Recorded per job so a reader can tell an attributable
    #: submission from a self-declared one, instead of having to know how the
    #: service happened to be deployed when it arrived.
    requested_by_verified: bool = False
    material: str | None = Field(default=None, max_length=60)
    original_filename: str
    artifact_kind: ArtifactKind
    byte_size: int
    sha256: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    approved_by: str | None = None
    approved_by_verified: bool = False
    approved_at: datetime | None = None
    #: True once the stored artifact has been deleted (cancellation). The job
    #: record outlives its file so the audit trail survives, but nothing can be
    #: run from it afterwards.
    artifact_removed: bool = False
    estimated_duration_minutes: float | None = None
    facts: ArtifactFacts | None = None
    verdict: ValidationVerdict | None = None
    history: list[StateTransition] = Field(default_factory=list)

    @field_validator("requested_by", "material")
    @classmethod
    def clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = _CONTROL_CHARS.sub("", value).strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class SubmissionStore:
    """Durable, in-process store for submissions.

    Jobs are held in memory and mirrored to one JSON file each, so a service
    restart does not silently empty a machine's queue. Every mutation goes
    through one lock: intake writes and state changes are concurrent by nature
    (an upload and an approval can overlap) and the queue's order must not
    depend on which one won a race.
    """

    def __init__(self, settings: SubmissionSettings) -> None:
        self._settings = settings
        self._root = settings.directory
        self._jobs: dict[str, SubmissionJob] = {}
        self._lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def settings(self) -> SubmissionSettings:
        return self._settings

    def load(self) -> None:
        """Read persisted jobs from disk. Called once at startup."""

        self._root.mkdir(parents=True, exist_ok=True)
        for path in sorted(self._root.glob("*.json")):
            try:
                job = SubmissionJob.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                # One unreadable record must not stop the service from serving
                # the rest of the queue.
                logger.warning("Ignoring unreadable submission record %s", path.name)
                continue
            self._jobs[job.submission_id] = job
        logger.info("Loaded %d submission(s) from %s", len(self._jobs), self._root)

    # -- reads -------------------------------------------------------------

    def get(self, submission_id: str) -> SubmissionJob | None:
        return self._jobs.get(submission_id)

    def list(
        self,
        *,
        machine: str | None = None,
        state: str | None = None,
    ) -> list[SubmissionJob]:
        jobs = [
            job
            for job in self._jobs.values()
            if (machine is None or job.target_machine == machine)
            and (state is None or job.state == state)
        ]
        return sorted(jobs, key=lambda job: (job.created_at, job.submission_id))

    def queue_for(self, machine: str) -> list[SubmissionJob]:
        """Jobs waiting on one machine, in the order they were submitted."""

        return [job for job in self.list(machine=machine) if job.state in QUEUED_STATES]

    def artifact_path(self, job: SubmissionJob) -> Path:
        """Internal only -- never serialise this into a response."""

        return self._root / f"{job.submission_id}.{job.artifact_kind}"

    # -- writes ------------------------------------------------------------

    async def accept(
        self,
        *,
        chunks: AsyncIterator[bytes],
        extension: str,
        target_machine: str,
        requested_by: str,
        requested_by_verified: bool = False,
        material: str | None,
        original_filename: str,
    ) -> SubmissionJob:
        """Persist an uploaded artifact and register it as a ``submitted`` job."""

        kind = ARTIFACT_EXTENSIONS.get(extension.lower())
        if kind is None:
            raise SubmissionError(f"unsupported artifact extension {extension!r}")
        owner = clean_text(requested_by, field="requested_by", required=True)
        filament = clean_text(material, field="material", required=False)

        self._root.mkdir(parents=True, exist_ok=True)
        submission_id = uuid.uuid4().hex
        target = self._root / f"{submission_id}.{kind}"
        partial = self._root / f"{submission_id}.part"

        digest = hashlib.sha256()
        size = 0
        try:
            handle = await asyncio.to_thread(partial.open, "wb")
            try:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self._settings.max_file_bytes:
                        raise ArtifactTooLarge(
                            f"artifact exceeds the {self._settings.max_file_bytes} byte limit"
                        )
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
            if size == 0:
                raise SubmissionError("the uploaded artifact is empty")
            await asyncio.to_thread(partial.replace, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

        now = datetime.now(UTC)
        job = SubmissionJob(
            submission_id=submission_id,
            target_machine=target_machine,
            requested_by=owner,  # type: ignore[arg-type]
            requested_by_verified=requested_by_verified,
            material=filament,
            original_filename=safe_display_name(original_filename),
            artifact_kind=kind,
            byte_size=size,
            sha256=digest.hexdigest(),
            state="submitted",
            created_at=now,
            updated_at=now,
            history=[StateTransition(at=now, from_state=None, to_state="submitted")],
        )
        async with self._lock:
            self._jobs[submission_id] = job
            await self._persist(job)
        return job

    async def transition(
        self, job: SubmissionJob, to_state: JobState, *, note: str | None = None
    ) -> SubmissionJob:
        async with self._lock:
            return await self._transition_locked(job.submission_id, to_state, note)

    async def record_validation(
        self,
        job: SubmissionJob,
        facts: ArtifactFacts | None,
        verdict: ValidationVerdict,
    ) -> SubmissionJob:
        async with self._lock:
            current = self._require(job.submission_id)
            updated = current.model_copy(
                update={
                    "facts": facts,
                    "verdict": verdict,
                    "estimated_duration_minutes": (
                        facts.estimated_duration_minutes if facts else None
                    ),
                    "updated_at": datetime.now(UTC),
                }
            )
            self._jobs[updated.submission_id] = updated
            await self._persist(updated)
            return updated

    async def approve(
        self, job: SubmissionJob, *, approved_by: str, verified: bool = False
    ) -> SubmissionJob:
        """Record the human sign-off that gates dispatch.

        Approval is a *record*, not an action: it moves no hardware and starts
        nothing. It flips ``verdict.dispatch_ready``, which is the flag a future
        dispatch step would require.
        """

        async with self._lock:
            current = self._require(job.submission_id)
            if current.state != "queued":
                raise InvalidTransition(
                    f"only a queued submission can be approved; {current.submission_id} "
                    f"is {current.state}"
                )
            if current.verdict is None or current.verdict.verdict != "pass":
                raise InvalidTransition(
                    "a submission that did not pass validation cannot be approved"
                )
            now = datetime.now(UTC)
            verdict = current.verdict.model_copy(update={"dispatch_ready": True})
            updated = current.model_copy(
                update={
                    "verdict": verdict,
                    "approved_by": _CONTROL_CHARS.sub("", approved_by).strip()[:120],
                    "approved_by_verified": verified,
                    "approved_at": now,
                }
            )
            self._jobs[updated.submission_id] = updated
            return await self._transition_locked(
                updated.submission_id,
                "approved",
                f"approved by {updated.approved_by}{_verified_suffix(verified)}",
            )

    async def cancel(
        self,
        job: SubmissionJob,
        *,
        cancelled_by: str,
        reason: str | None = None,
        verified: bool = False,
    ) -> SubmissionJob:
        """Withdraw a waiting job from its machine's queue.

        Legal only from the states in :data:`CANCELLABLE_STATES`. This is a
        queue operation, **not** an abort: it can never reach a printer, and it
        is deliberately not offered for a job that has been dispatched --
        stopping a running print is a control-plane action that needs a claim.

        The stored artifact is deleted, because a withdrawn job has no further
        use for it and it is the submitter's data. The job record stays, so the
        decision and its reason remain auditable; ``artifact_removed`` marks
        that the file is gone. There is no undo -- a withdrawn job is
        resubmitted, not revived.
        """

        async with self._lock:
            current = self._require(job.submission_id)
            if current.state not in CANCELLABLE_STATES:
                raise InvalidTransition(
                    f"only a waiting submission can be cancelled; "
                    f"{current.submission_id} is {current.state}"
                )

            path = self.artifact_path(current)
            await asyncio.to_thread(path.unlink, True)

            who = clean_text(cancelled_by, field="cancelled_by", required=True)
            why = clean_text(reason, field="reason", required=False)
            update: dict[str, object] = {"artifact_removed": True}
            if current.verdict is not None:
                # An approved job carries dispatch_ready; withdrawing it must
                # retract that, or the record would still read as cleared to run.
                update["verdict"] = current.verdict.model_copy(
                    update={"dispatch_ready": False}
                )
            self._jobs[current.submission_id] = current.model_copy(update=update)

            note = f"cancelled by {who}{_verified_suffix(verified)}"
            if why:
                note = f"{note}: {why}"
            return await self._transition_locked(
                current.submission_id, "cancelled", note
            )

    # -- internals ---------------------------------------------------------

    def _require(self, submission_id: str) -> SubmissionJob:
        job = self._jobs.get(submission_id)
        if job is None:
            raise InvalidTransition(f"unknown submission {submission_id}")
        return job

    async def _transition_locked(
        self, submission_id: str, to_state: JobState, note: str | None
    ) -> SubmissionJob:
        current = self._require(submission_id)
        if to_state not in ALLOWED_TRANSITIONS[current.state]:
            raise InvalidTransition(
                f"{submission_id} cannot move from {current.state} to {to_state}"
            )
        now = datetime.now(UTC)
        updated = current.model_copy(
            update={
                "state": to_state,
                "updated_at": now,
                "history": [
                    *current.history,
                    StateTransition(
                        at=now, from_state=current.state, to_state=to_state, note=note
                    ),
                ],
            }
        )
        self._jobs[submission_id] = updated
        await self._persist(updated)
        return updated

    async def _persist(self, job: SubmissionJob) -> None:
        payload = job.model_dump_json(indent=2)
        target = self._root / f"{job.submission_id}.json"
        temporary = self._root / f"{job.submission_id}.json.tmp"

        def write() -> None:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(target)

        await asyncio.to_thread(write)


async def run_validation(
    store: SubmissionStore,
    job: SubmissionJob,
    profile: MachineProfile,
) -> SubmissionJob:
    """Validate one submitted job against its target machine's profile.

    Inspection runs in a worker thread because it reads a potentially large file
    and the event loop is also serving status polls. A submission that passes is
    enqueued for its machine; one that fails is rejected with the failing checks
    recorded, which is a terminal outcome.
    """

    job = await store.transition(store.get(job.submission_id) or job, "validating")
    path = store.artifact_path(job)
    try:
        facts = await asyncio.to_thread(
            inspect_artifact, path, scan_max_bytes=store.settings.scan_max_bytes
        )
    except Exception as exc:
        # `ArtifactError` messages are written here and carry no path, so they
        # are safe to relay and are the useful half of the answer. Anything else
        # is reported by exception type only: an unexpected error can quote the
        # stored path, which the client never named and has no use for.
        reason = (
            str(exc) if isinstance(exc, ArtifactError) else type(exc).__name__
        )
        logger.warning(
            "Artifact inspection failed for %s (%s)", job.submission_id, type(exc).__name__
        )
        verdict = ValidationVerdict(
            submission_id=job.submission_id,
            verdict="reject",
            checks=[
                CheckResult(
                    check="artifact_readable",
                    status="fail",
                    ok=False,
                    detail=f"the submitted {job.artifact_kind} could not be read: {reason}",
                )
            ],
            reasons=["artifact_readable"],
            machine=profile.id,
        )
        job = await store.record_validation(job, None, verdict)
        return await store.transition(job, "rejected", note="artifact_readable")

    verdict = validate_model(
        submission_id=job.submission_id,
        facts=facts,
        profile=profile,
        requested_material=job.material,
    )
    job = await store.record_validation(job, facts, verdict)
    if verdict.verdict == "reject":
        return await store.transition(job, "rejected", note=", ".join(verdict.reasons))
    job = await store.transition(job, "validated")
    return await store.transition(job, "queued")


async def dispatch(job: SubmissionJob) -> NoReturn:
    """The gated step. Not implemented, and deliberately unreachable.

    Dispatching means uploading the artifact to a printer and starting a print:
    the single printer-touching action in the whole pipeline. Shipping it
    requires the approved control-plane design (``docs/CONTROL_PLANE_DESIGN.md``)
    -- the v1.1 claim protocol, per-action preconditions with structured 412
    refusals, and the audited approval model -- none of which exists yet.

    No HTTP route calls this function. It exists so the boundary has a name and
    a test, not as a switch waiting to be flipped.
    """

    raise DispatchUnavailable(
        f"dispatch is not implemented: submission {job.submission_id} stops at the "
        "approval gate until the control-plane design ships"
    )
