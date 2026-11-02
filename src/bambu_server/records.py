"""Durable print records and the optional central-ingest outbox.

Follows the lab's OT-2 gateway pattern:

* one schema-versioned **result bundle** per submission at
  ``<submissions>/results/<submission_id>.json``, written atomically (temp file
  then ``os.replace``) when the job reaches a terminal state, and never pruned;
* an on-disk **outbox** at ``<submissions>/outbox/<submission_id>.json`` whose
  entries move ``pending -> delivered -> filing -> filed`` (or ``held``, which
  needs a person), or sit at ``local_only`` when no ELN project was chosen or no
  central ingest is configured;
* a **worker** that POSTs bundles to the central dashboard's ingest URL with a
  bearer token and polls ``GET {url}/{device_id}/{submission_id}`` for filing
  status, retrying with exponential backoff, and resuming after a restart.

Devices never write to the ELN directly: the only destination is the central
server's ``/api/ingest/*``, which alone holds ELN credentials. Nothing here
touches a printer, and every failure is logged rather than raised into dispatch
or monitoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from .agent_workflow import (
    TERMINAL_EVENT_TYPES,
    EventHub,
    PickupReadiness,
    pickup_readiness,
)
from .artifacts import ObjectPosition, inspect_object_positions
from .submissions import (
    TERMINAL_STATES,
    PlateCheck,
    SubmissionJob,
    SubmissionStore,
)

if TYPE_CHECKING:
    from .monitor import PrinterMonitor

logger = logging.getLogger(__name__)

RESULT_SCHEMA_ID = "ac.bambu.print_result"
RESULT_SCHEMA_VERSION = 1
OUTBOX_SCHEMA_VERSION = 1
INGEST_PAYLOAD_SCHEMA_VERSION = 1

#: Largest response body the worker reads from the central server.
_MAX_RESPONSE_BYTES = 1 << 20


# -- result bundle -------------------------------------------------------------


class ArtifactRecord(BaseModel):
    kind: str
    original_filename: str
    byte_size: int
    sha256: str
    #: Whether the exact file is still stored and downloadable.
    stored: bool
    download_path: str


class Timeline(BaseModel):
    submitted_at: datetime
    approved_at: datetime | None = None
    dispatch_requested_at: datetime | None = None
    command_published_at: datetime | None = None
    observed_running_at: datetime | None = None
    observed_finished_at: datetime | None = None
    terminal_at: datetime | None = None


class ResultBundle(BaseModel):
    """The durable record of one submission, stable for downstream filing.

    ``final`` is True for the bundle written when the job reached a terminal
    state; a bundle built on request for a job still in play is not final.
    ``objects`` are bed-frame placements from the sliced file and ``pickup`` is
    the bed temperature readiness observed when the job finished.
    """

    schema_id: str = RESULT_SCHEMA_ID
    schema_version: int = RESULT_SCHEMA_VERSION
    gateway_id: str
    gateway_version: str
    printer_id: str
    submission_id: str
    state: str
    final: bool
    generated_at: datetime
    eln_project: str | None = None
    artifact: ArtifactRecord
    timeline: Timeline
    plate_check: PlateCheck | None = None
    bed_size_mm: tuple[float, float] | None = None
    objects: list[ObjectPosition] = Field(default_factory=list)
    pickup: PickupReadiness | None = None
    job: SubmissionJob


def build_bundle(
    job: SubmissionJob,
    *,
    gateway_id: str,
    gateway_version: str,
    artifact_stored: bool,
    objects: tuple[ObjectPosition, ...] | list[ObjectPosition],
    pickup: PickupReadiness | None,
    bed_size_mm: tuple[float, float] | None,
    final: bool,
) -> ResultBundle:
    dispatch = job.dispatch
    terminal_at = next(
        (step.at for step in reversed(job.history) if step.to_state in TERMINAL_STATES), None
    )
    return ResultBundle(
        gateway_id=gateway_id,
        gateway_version=gateway_version,
        printer_id=job.target_machine,
        submission_id=job.submission_id,
        state=job.state,
        final=final,
        generated_at=datetime.now(UTC),
        eln_project=job.eln_project,
        artifact=ArtifactRecord(
            kind=job.artifact_kind,
            original_filename=job.original_filename,
            byte_size=job.byte_size,
            sha256=job.sha256,
            stored=artifact_stored,
            download_path=f"/submissions/{job.submission_id}/artifact",
        ),
        timeline=Timeline(
            submitted_at=job.created_at,
            approved_at=job.approved_at,
            dispatch_requested_at=dispatch.requested_at if dispatch else None,
            command_published_at=dispatch.command_published_at if dispatch else None,
            observed_running_at=dispatch.observed_running_at if dispatch else None,
            observed_finished_at=dispatch.observed_finished_at if dispatch else None,
            terminal_at=terminal_at,
        ),
        plate_check=dispatch.plate_check if dispatch else None,
        bed_size_mm=bed_size_mm,
        objects=list(objects),
        pickup=pickup,
        job=job,
    )


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


class ResultStore:
    """One bundle file per submission. Never pruned, not even by ``DELETE``."""

    def __init__(self, root: Path) -> None:
        self.root = root / "results"

    def path(self, submission_id: str) -> Path:
        return self.root / f"{submission_id}.json"

    def exists(self, submission_id: str) -> bool:
        return self.path(submission_id).is_file()

    def read(self, submission_id: str) -> bytes | None:
        try:
            return self.path(submission_id).read_bytes()
        except OSError:
            return None

    async def write(self, bundle: ResultBundle) -> None:
        await asyncio.to_thread(
            _atomic_write, self.path(bundle.submission_id), bundle.model_dump_json(indent=2)
        )


# -- outbox ----------------------------------------------------------------------

OutboxState = Literal["pending", "delivered", "filing", "filed", "held", "local_only"]
OUTBOX_STATES: tuple[str, ...] = ("pending", "delivered", "filing", "filed", "held", "local_only")
_ACTIVE_STATES = frozenset({"pending", "delivered", "filing"})


class OutboxEntry(BaseModel):
    schema_version: int = OUTBOX_SCHEMA_VERSION
    submission_id: str
    device_id: str
    state: OutboxState
    eln_project: str | None = None
    #: Why the entry is ``local_only``: ``no_eln_project`` or ``ingest_not_configured``.
    local_reason: str | None = None
    queued_by: str | None = None
    queued_by_verified: bool = False
    created_at: datetime
    updated_at: datetime
    #: Consecutive failed attempts; reset by any success.
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    delivered_at: datetime | None = None
    filed_at: datetime | None = None
    remote_state: str | None = None
    remote_reference: str | None = None


class OutboxConflict(RuntimeError):
    """An ingest request the outbox will not accept in the entry's state."""


class OutboxStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "outbox"
        self._entries: dict[str, OutboxEntry] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.root.glob("*.json")):
            try:
                entry = OutboxEntry.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Ignoring unreadable outbox entry %s", path.name)
                continue
            self._entries[entry.submission_id] = entry

    def get(self, submission_id: str) -> OutboxEntry | None:
        return self._entries.get(submission_id)

    def entries(self, state: str | None = None) -> list[OutboxEntry]:
        found = [e for e in self._entries.values() if state is None or e.state == state]
        return sorted(found, key=lambda e: (e.updated_at, e.submission_id), reverse=True)

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(OUTBOX_STATES, 0)
        for entry in self._entries.values():
            counts[entry.state] += 1
        return counts

    def due(self, now: datetime) -> list[OutboxEntry]:
        return [
            entry
            for entry in self._entries.values()
            if entry.state in _ACTIVE_STATES
            and (entry.next_attempt_at is None or entry.next_attempt_at <= now)
        ]

    async def put(self, entry: OutboxEntry) -> OutboxEntry:
        async with self._lock:
            await asyncio.to_thread(
                _atomic_write,
                self.root / f"{entry.submission_id}.json",
                entry.model_dump_json(indent=2),
            )
            self._entries[entry.submission_id] = entry
            return entry

    async def ensure_for_terminal(
        self, job: SubmissionJob, *, ingest_configured: bool
    ) -> OutboxEntry:
        """Create the job's entry once; an existing entry is left as it is."""

        existing = self._entries.get(job.submission_id)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        if job.eln_project and ingest_configured:
            entry = OutboxEntry(
                submission_id=job.submission_id,
                device_id=job.target_machine,
                state="pending",
                eln_project=job.eln_project,
                queued_by=job.approved_by or job.requested_by,
                queued_by_verified=(
                    job.approved_by_verified if job.approved_by else job.requested_by_verified
                ),
                created_at=now,
                updated_at=now,
                next_attempt_at=now,
            )
        else:
            entry = OutboxEntry(
                submission_id=job.submission_id,
                device_id=job.target_machine,
                state="local_only",
                eln_project=job.eln_project,
                local_reason="ingest_not_configured" if job.eln_project else "no_eln_project",
                created_at=now,
                updated_at=now,
            )
        return await self.put(entry)

    async def queue(
        self,
        job: SubmissionJob,
        *,
        eln_project: str,
        queued_by: str,
        verified: bool,
        ingest_configured: bool,
    ) -> OutboxEntry:
        """Queue a terminal job for central ingest under a chosen project."""

        if job.state not in TERMINAL_STATES:
            raise OutboxConflict(
                f"only a job that has finished going anywhere can be filed; it is {job.state}"
            )
        if not ingest_configured:
            raise OutboxConflict(
                "central ingest is not configured on this gateway; the record stays local_only"
            )
        existing = self._entries.get(job.submission_id)
        if existing is not None and existing.state in {"pending", "delivered", "filing", "filed"}:
            raise OutboxConflict(f"this job's record is already {existing.state}")
        now = datetime.now(UTC)
        entry = OutboxEntry(
            submission_id=job.submission_id,
            device_id=job.target_machine,
            state="pending",
            eln_project=eln_project,
            queued_by=queued_by,
            queued_by_verified=verified,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            next_attempt_at=now,
        )
        return await self.put(entry)


# -- worker ----------------------------------------------------------------------

#: ``(method, url, headers, body, timeout_s) -> (status, response body)``.
#: Raises ``OSError`` when no HTTP response was received.
Transport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator URL
            return response.status, response.read(_MAX_RESPONSE_BYTES)
    except HTTPError as exc:
        return exc.code, exc.read(_MAX_RESPONSE_BYTES) if exc.fp else b""


def _retryable(status: int) -> bool:
    return status in (408, 425, 429) or status >= 500


def _remote_fields(body: bytes) -> tuple[str | None, str | None, str | None]:
    """``(state, reference, detail)`` from a central server JSON response."""

    try:
        data = json.loads(body.decode("utf-8", errors="replace")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    state = data.get("state") or data.get("status")
    reference = data.get("eln_entry_url") or data.get("reference") or data.get("entry_url")
    detail = data.get("detail") or data.get("reason")
    return (
        str(state).strip().lower() if state else None,
        str(reference)[:500] if reference else None,
        str(detail)[:500] if detail else None,
    )


class OutboxWorker:
    """Delivers pending bundles and follows their filing until a final state."""

    def __init__(
        self,
        *,
        outbox: OutboxStore,
        results: ResultStore,
        url: str,
        token: str,
        gateway_id: str,
        poll_interval_s: float,
        retry_initial_s: float,
        retry_max_s: float,
        request_timeout_s: float,
        transport: Transport = urllib_transport,
    ) -> None:
        self._outbox = outbox
        self._results = results
        self._url = url
        self._token = token
        self._gateway_id = gateway_id
        self._poll_s = poll_interval_s
        self._retry_initial_s = retry_initial_s
        self._retry_max_s = retry_max_s
        self._timeout_s = request_timeout_s
        self._transport = transport

    async def run(self, tick_s: float) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Results outbox tick failed; retrying")
            await asyncio.sleep(tick_s)

    async def tick(self, now: datetime | None = None) -> None:
        for entry in self._outbox.due(now or datetime.now(UTC)):
            try:
                await self._advance(entry)
            except Exception:
                logger.exception("Outbox entry %s could not advance", entry.submission_id)

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _call(self, method: str, url: str, body: bytes | None) -> tuple[int, bytes]:
        return await asyncio.to_thread(
            self._transport, method, url, self._headers(json_body=body is not None),
            body, self._timeout_s,
        )

    async def _advance(self, entry: OutboxEntry) -> None:
        if entry.state == "pending":
            await self._deliver(entry)
        else:
            await self._poll(entry)

    async def _deliver(self, entry: OutboxEntry) -> None:
        bundle = self._results.read(entry.submission_id)
        if bundle is None:
            await self._failed(entry, "the result bundle is not on disk yet")
            return
        payload = {
            "schema_version": INGEST_PAYLOAD_SCHEMA_VERSION,
            "device_id": entry.device_id,
            "gateway_id": self._gateway_id,
            "id": entry.submission_id,
            "eln_project": entry.eln_project,
            "bundle": json.loads(bundle),
        }
        try:
            status, body = await self._call("POST", self._url, json.dumps(payload).encode())
        except OSError as exc:
            await self._failed(entry, f"central ingest unreachable ({type(exc).__name__})")
            return
        if 200 <= status < 300 or status == 409:
            now = datetime.now(UTC)
            state, reference, detail = _remote_fields(body)
            updated = entry.model_copy(update={
                "state": "delivered", "delivered_at": now, "updated_at": now,
                "attempts": 0, "last_error": None, "remote_state": state,
                "next_attempt_at": now + timedelta(seconds=self._poll_s),
            })
            await self._outbox.put(self._apply_remote(updated, state, reference, detail))
        elif _retryable(status):
            await self._failed(entry, f"central ingest answered HTTP {status}")
        else:
            await self._hold(entry, f"central ingest refused the bundle (HTTP {status})")

    async def _poll(self, entry: OutboxEntry) -> None:
        url = f"{self._url}/{entry.device_id}/{entry.submission_id}"
        try:
            status, body = await self._call("GET", url, None)
        except OSError as exc:
            await self._failed(entry, f"central ingest unreachable ({type(exc).__name__})")
            return
        now = datetime.now(UTC)
        if status == 404:
            # The central server has no record of it: deliver it again.
            await self._outbox.put(entry.model_copy(update={
                "state": "pending", "updated_at": now, "next_attempt_at": now,
                "last_error": "central ingest had no record of the delivered bundle",
            }))
            return
        if _retryable(status):
            await self._failed(entry, f"filing status answered HTTP {status}")
            return
        if not 200 <= status < 300:
            await self._hold(entry, f"filing status refused (HTTP {status})")
            return
        state, reference, detail = _remote_fields(body)
        updated = entry.model_copy(update={
            "updated_at": now, "attempts": 0, "last_error": None,
            "remote_state": state or entry.remote_state,
            "next_attempt_at": now + timedelta(seconds=self._poll_s),
        })
        await self._outbox.put(self._apply_remote(updated, state, reference, detail))

    def _apply_remote(
        self, entry: OutboxEntry, state: str | None, reference: str | None, detail: str | None
    ) -> OutboxEntry:
        update: dict[str, object] = {}
        if reference:
            update["remote_reference"] = reference
        if state == "filing":
            update["state"] = "filing"
        elif state == "filed":
            update.update(state="filed", filed_at=entry.updated_at, next_attempt_at=None)
        elif state == "held":
            update.update(
                state="held", next_attempt_at=None,
                last_error=detail or "held by the central server for a person to review",
            )
        return entry.model_copy(update=update) if update else entry

    async def _failed(self, entry: OutboxEntry, error: str) -> None:
        attempts = entry.attempts + 1
        delay = min(self._retry_max_s, self._retry_initial_s * 2 ** (attempts - 1))
        now = datetime.now(UTC)
        logger.warning(
            "Outbox entry %s attempt %d failed: %s", entry.submission_id, attempts, error
        )
        await self._outbox.put(entry.model_copy(update={
            "attempts": attempts, "last_error": error, "updated_at": now,
            "next_attempt_at": now + timedelta(seconds=delay),
        }))

    async def _hold(self, entry: OutboxEntry, error: str) -> None:
        logger.warning("Outbox entry %s held: %s", entry.submission_id, error)
        await self._outbox.put(entry.model_copy(update={
            "state": "held", "last_error": error, "updated_at": datetime.now(UTC),
            "next_attempt_at": None,
        }))


# -- record keeper ---------------------------------------------------------------


@dataclass(frozen=True)
class _Work:
    job: SubmissionJob
    emit: bool
    recovered: bool = False


class RecordKeeper:
    """Turns job state changes into bundles, outbox entries and events.

    The submission store calls :meth:`notify` synchronously after each persisted
    transition; the work happens later on this keeper's own task, so neither a
    slow artifact read nor a failure here can delay or break dispatch.
    """

    def __init__(
        self,
        *,
        store: SubmissionStore,
        results: ResultStore,
        outbox: OutboxStore,
        events: EventHub,
        monitors: dict[str, PrinterMonitor],
        gateway_id: str,
        gateway_version: str,
        pickup_safe_bed_c: float,
        ingest_configured: bool,
        cooldown_poll_s: float,
        cooldown_timeout_s: float,
    ) -> None:
        self._store = store
        self._results = results
        self._outbox = outbox
        self._events = events
        self._monitors = monitors
        self._gateway_id = gateway_id
        self._gateway_version = gateway_version
        self._safe_bed_c = pickup_safe_bed_c
        self.ingest_configured = ingest_configured
        self._cooldown_poll_s = cooldown_poll_s
        self._cooldown_timeout_s = cooldown_timeout_s
        self._queue: asyncio.Queue[_Work] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._cooldowns: set[asyncio.Task[None]] = set()

    def notify(self, job: SubmissionJob) -> None:
        if job.state == "running" or job.state in TERMINAL_STATES:
            self._queue.put_nowait(_Work(job=job, emit=True))

    async def start(self) -> None:
        await self._safe_publish(
            "gateway.restarted", note="gateway process started; event ids continue"
        )
        recovered = {job.submission_id for job in self._store.recovered}
        for job in self._store.recovered:
            self._queue.put_nowait(_Work(job=job, emit=True, recovered=True))
        # Terminal jobs from before bundles existed get one, quietly.
        for job in self._store.list():
            if (
                job.state in TERMINAL_STATES
                and job.submission_id not in recovered
                and not self._results.exists(job.submission_id)
            ):
                self._queue.put_nowait(_Work(job=job, emit=False))
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._cooldowns) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001 - shutting down
                pass
        self._task = None
        self._cooldowns.clear()

    async def drain(self) -> None:
        """Wait until every queued change has been processed (tests, shutdown)."""

        await self._queue.join()

    async def _run(self) -> None:
        while True:
            work = await self._queue.get()
            try:
                await self._process(work)
            except Exception:
                logger.exception("Could not record %s", work.job.submission_id)
            finally:
                self._queue.task_done()

    # -- helpers used by routes too ------------------------------------------

    def readiness(self, printer_id: str) -> PickupReadiness | None:
        monitor = self._monitors.get(printer_id)
        if monitor is None:
            return None
        return pickup_readiness(monitor.status(), self._safe_bed_c)

    def bed_size(self, printer_id: str) -> tuple[float, float] | None:
        monitor = self._monitors.get(printer_id)
        return monitor.definition.profile.bed_size_mm if monitor else None

    def artifact_stored(self, job: SubmissionJob) -> bool:
        return not job.artifact_removed and self._store.artifact_path(job).is_file()

    async def positions(self, job: SubmissionJob) -> tuple[ObjectPosition, ...]:
        if not self.artifact_stored(job):
            return ()
        try:
            return await asyncio.to_thread(
                inspect_object_positions,
                self._store.artifact_path(job),
                scan_max_bytes=self._store.settings.scan_max_bytes,
            )
        except Exception:
            logger.warning("Object positions unavailable for %s", job.submission_id)
            return ()

    def bundle(
        self,
        job: SubmissionJob,
        *,
        objects: tuple[ObjectPosition, ...],
        pickup: PickupReadiness | None,
        final: bool,
    ) -> ResultBundle:
        return build_bundle(
            job,
            gateway_id=self._gateway_id,
            gateway_version=self._gateway_version,
            artifact_stored=self.artifact_stored(job),
            objects=objects,
            pickup=pickup,
            bed_size_mm=self.bed_size(job.target_machine),
            final=final,
        )

    def results_exists(self, submission_id: str) -> bool:
        return self._results.exists(submission_id)

    async def write_final_bundle(self, job: SubmissionJob) -> None:
        """Write a terminal job's bundle now, if the keeper has not yet."""

        objects = await self.positions(job)
        await self._results.write(self.bundle(job, objects=objects, pickup=None, final=True))

    # -- processing ----------------------------------------------------------

    async def _process(self, work: _Work) -> None:
        job = work.job
        objects = await self.positions(job)
        if job.state == "running":
            await self._safe_publish("job.started", **self._job_fields(job, objects))
            return

        pickup = self.readiness(job.target_machine) if job.state == "finished" else None
        try:
            await self._results.write(self.bundle(job, objects=objects, pickup=pickup, final=True))
        except Exception:
            logger.exception("Could not write the result bundle for %s", job.submission_id)
        try:
            await self._outbox.ensure_for_terminal(job, ingest_configured=self.ingest_configured)
        except Exception:
            logger.exception("Could not create the outbox entry for %s", job.submission_id)
        if work.emit:
            await self._safe_publish(
                TERMINAL_EVENT_TYPES[job.state],
                recovered_after_restart=work.recovered,
                pickup=pickup,
                **self._job_fields(job, objects),
            )
        if job.state == "finished" and work.emit and pickup is not None and not pickup.cooled:
            task = asyncio.create_task(self._await_cooling(job, objects))
            self._cooldowns.add(task)
            task.add_done_callback(self._cooldowns.discard)

    async def _await_cooling(self, job: SubmissionJob, objects: tuple[ObjectPosition, ...]) -> None:
        """Announce ``job.cooled`` once the cached bed temperature is safe."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._cooldown_timeout_s
        while loop.time() < deadline:
            await asyncio.sleep(self._cooldown_poll_s)
            pickup = self.readiness(job.target_machine)
            if pickup is not None and pickup.cooled:
                await self._safe_publish(
                    "job.cooled", pickup=pickup, **self._job_fields(job, objects)
                )
                return

    def _job_fields(self, job: SubmissionJob, objects: tuple[ObjectPosition, ...]) -> dict:
        return {
            "submission_id": job.submission_id,
            "printer_id": job.target_machine,
            "state": job.state,
            "updated_at": job.updated_at,
            "eln_project": job.eln_project,
            "note": job.history[-1].note if job.history else None,
            "object_positions": objects,
        }

    async def _safe_publish(self, event_type: str, **fields: object) -> None:
        try:
            await self._events.publish(event_type, **fields)  # type: ignore[arg-type]
        except Exception:
            logger.exception("Event %s was not published; the job is unaffected", event_type)
