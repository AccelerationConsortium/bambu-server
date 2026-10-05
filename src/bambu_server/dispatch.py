"""Dispatch: the gate in front of the printer, and the one step that goes through it.

What the lab's rules ask of a control plane meets here:

* **one predicate, two surfaces** -- :meth:`ControlGate.start_print_blockers`
  decides both whether ``start_print`` appears in ``allowed_actions`` and
  whether a request for it is refused with 412 (STATUS_SPEC §6.2);
* **a claim** on every control route (423 without a live token);
* **a human's statement** that the build plate is empty, recorded with how
  they looked and, for a camera check, the hash of the frame they saw;
* **revalidation** against the printer as it is now;
* **an explicit tray mapping**, checked against the loaded spools;
* **honest outcomes** -- ``running`` only once the printer reports it, and a
  step that ends without the printer settling the question is ``failed`` with
  the uncertainty written down. Nothing here retries.

Refusals never touch ``last_error`` (STATUS_SPEC §6.3).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .artifacts import ArtifactFacts
from .backend import PrinterBackend, PrinterCommandError, StartPrintCommand
from .claims import ClaimRegistry
from .config import DispatchSettings
from .identity import Actor
from .monitor import PrinterMonitor
from .plates import plate_label
from .profiles import MachineProfile
from .submissions import (
    DispatchRecord,
    InvalidTransition,
    PlateCheck,
    PlateCheckMethod,
    SubmissionJob,
    SubmissionStore,
    TrayAssignment,
)
from .validation import validate_model

logger = logging.getLogger(__name__)

#: A camera frame is evidence of the plate only briefly.
SNAPSHOT_MAX_AGE_S = 300.0
_SNAPSHOT_RETENTION_S = 3600.0
_REMOTE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ControlRefusal(Exception):
    """A control request the gateway will not carry out."""

    def __init__(self, status: int, body: dict[str, object]) -> None:
        super().__init__(str(body.get("detail", "refused")))
        self.status = status
        self.body = body


class StartPrintRequest(BaseModel):
    submission_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    #: Must be literally true, so the record carries an affirmative statement.
    plate_confirmed_empty: Literal[True]
    plate_check_method: PlateCheckMethod
    snapshot_token: str | None = Field(default=None, max_length=64)
    ams_mapping: list[TrayAssignment] = Field(min_length=1, max_length=16)
    dispatched_by: str | None = Field(default=None, min_length=1, max_length=120)


class StopPrintRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
    stopped_by: str | None = Field(default=None, min_length=1, max_length=120)


class SnapshotResponse(BaseModel):
    token: str
    printer_id: str
    captured_at: datetime
    sha256: str
    content_type: Literal["image/jpeg"] = "image/jpeg"
    image_base64: str


class StopPrintResponse(BaseModel):
    printer_id: str
    stop_requested_at: datetime
    submission_id: str | None = None
    note: str


def _metric(status: object, name: str) -> float | None:
    metric = (getattr(status, "metrics", {}) or {}).get(name)
    if metric is None:
        return None
    value = metric.value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class ControlGate:
    """Device-level preconditions shared by ``allowed_actions`` and the routes."""

    def __init__(self, settings: DispatchSettings) -> None:
        self.settings = settings

    def start_print_blockers(
        self, monitor: PrinterMonitor, *, busy_with_dispatch: bool
    ) -> list[dict[str, object]]:
        status = monitor.status()
        if status.activity == "unknown":
            return [
                {
                    "detail": "printer state is not determinable (telemetry missing or stale)",
                    "activity": "unknown",
                    "retry_after_s": 10,
                }
            ]
        blockers: list[dict[str, object]] = []
        if status.activity != "idle":
            blockers.append(
                {"detail": "a print job is already in flight", "activity": status.activity}
            )
        if status.equipment_status == "error":
            blockers.append(
                {
                    "detail": "the printer reports a failed job; clear it on the printer first",
                    "equipment_status": "error",
                    "last_error_code": status.last_error.code if status.last_error else None,
                }
            )
        bed = _metric(status, "bed_temperature")
        nozzle = _metric(status, "nozzle_temperature")
        if bed is None or nozzle is None:
            blockers.append(
                {
                    "detail": "bed or nozzle temperature is not reported",
                    "bed_c": bed,
                    "nozzle_c": nozzle,
                }
            )
        elif bed > self.settings.safe_bed_c or nozzle > self.settings.safe_nozzle_c:
            blockers.append(
                {
                    "detail": "the printer is not cold enough to start a new job",
                    "bed_c": bed,
                    "nozzle_c": nozzle,
                    "limit_bed_c": self.settings.safe_bed_c,
                    "limit_nozzle_c": self.settings.safe_nozzle_c,
                    "retry_after_s": 60,
                }
            )
        if monitor.definition.profile.plate is None:
            blockers.append(
                {"detail": "no build plate is declared for this printer", "machine_plate": None}
            )
        if busy_with_dispatch:
            blockers.append(
                {
                    "detail": "the gateway is already tracking a job on this printer",
                    "gateway_job_in_flight": True,
                }
            )
        return blockers

    def stop_print_blockers(self, monitor: PrinterMonitor) -> list[dict[str, object]]:
        status = monitor.status()
        if status.activity != "running":
            return [{"detail": "no print job is in flight", "activity": status.activity}]
        return []

    def allowed_actions(self, monitor: PrinterMonitor, *, busy_with_dispatch: bool) -> list[str]:
        if not self.settings.enabled:
            return []
        actions = ["snapshot"]
        if not self.start_print_blockers(monitor, busy_with_dispatch=busy_with_dispatch):
            actions.append("start_print")
        if not self.stop_print_blockers(monitor):
            actions.append("stop_print")
        return actions


@dataclass
class _Snapshot:
    token: str
    printer_id: str
    session_id: str | None
    captured_at: datetime
    sha256: str
    path: Path


@dataclass
class _PrinterControl:
    claims: ClaimRegistry = field(default_factory=ClaimRegistry)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_job: str | None = None
    watcher: asyncio.Task[None] | None = None


class Dispatcher:
    """Per-printer control state, and the dispatch step itself."""

    def __init__(
        self,
        *,
        settings: DispatchSettings,
        store: SubmissionStore,
        monitors: dict[str, PrinterMonitor],
        backends: dict[str, PrinterBackend],
        poll_interval_s: float,
    ) -> None:
        self.settings = settings
        self.gate = ControlGate(settings)
        self._store = store
        self._monitors = monitors
        self._backends = backends
        self._poll_s = max(0.01, poll_interval_s)
        self._printers = {printer_id: _PrinterControl() for printer_id in monitors}
        self._snapshots: dict[str, _Snapshot] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Re-attach to jobs a previous process left ``running``."""

        for job in self._store.list(state="running"):
            control = self._printers.get(job.target_machine)
            if control is None:
                continue
            control.active_job = job.submission_id
            control.watcher = asyncio.create_task(self._watch_running(job.submission_id))

    async def stop(self) -> None:
        for control in self._printers.values():
            if control.watcher is not None:
                control.watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await control.watcher
                control.watcher = None

    # -- reads -------------------------------------------------------------

    def claims(self, printer_id: str) -> ClaimRegistry:
        return self._control(printer_id).claims

    def busy(self, printer_id: str) -> bool:
        control = self._printers.get(printer_id)
        return bool(control and control.active_job)

    def allowed_actions(self, printer_id: str) -> list[str]:
        monitor = self._monitors.get(printer_id)
        if monitor is None:
            return []
        return self.gate.allowed_actions(monitor, busy_with_dispatch=self.busy(printer_id))

    def require_claim(self, printer_id: str, token: str | None) -> None:
        control = self._control(printer_id)
        if not control.claims.verify(token):
            holder = control.claims.holder()
            raise ControlRefusal(
                423,
                {
                    "detail": "a live claim token is required on control routes",
                    "claimed_by": holder.model_dump(mode="json") if holder else None,
                },
            )

    def plate_snapshot_path(self, job: SubmissionJob) -> Path | None:
        path = self._store.root / f"{job.submission_id}.plate.jpg"
        return path if path.is_file() else None

    def _control(self, printer_id: str) -> _PrinterControl:
        control = self._printers.get(printer_id)
        if control is None:
            raise ControlRefusal(404, {"detail": "printer not configured"})
        return control

    # -- snapshot ----------------------------------------------------------

    async def snapshot(self, printer_id: str, *, claim_token: str | None) -> SnapshotResponse:
        self.require_claim(printer_id, claim_token)
        control = self._control(printer_id)
        try:
            image = await asyncio.to_thread(self._backends[printer_id].snapshot)
        except PrinterCommandError as exc:
            raise ControlRefusal(502, {"detail": f"camera snapshot failed: {exc}"}) from exc
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(24)
        digest = hashlib.sha256(image).hexdigest()
        directory = self._store.root / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{token}.jpg"
        await asyncio.to_thread(path.write_bytes, image)
        holder = control.claims.holder()
        self._snapshots[token] = _Snapshot(
            token=token,
            printer_id=printer_id,
            session_id=holder.session_id if holder else None,
            captured_at=now,
            sha256=digest,
            path=path,
        )
        self._prune_snapshots(now)
        return SnapshotResponse(
            token=token,
            printer_id=printer_id,
            captured_at=now,
            sha256=digest,
            image_base64=base64.b64encode(image).decode("ascii"),
        )

    def _prune_snapshots(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=_SNAPSHOT_RETENTION_S)
        for token, snap in list(self._snapshots.items()):
            if snap.captured_at < cutoff:
                snap.path.unlink(missing_ok=True)
                self._snapshots.pop(token, None)

    # -- start_print -------------------------------------------------------

    async def start_print(
        self,
        printer_id: str,
        request: StartPrintRequest,
        *,
        actor: Actor,
        claim_token: str | None,
    ) -> SubmissionJob:
        control = self._control(printer_id)
        self.require_claim(printer_id, claim_token)
        monitor = self._monitors[printer_id]
        backend = self._backends[printer_id]

        async with control.lock:
            job = self._store.get(request.submission_id)
            if job is None:
                raise ControlRefusal(404, {"detail": "unknown submission"})
            if job.target_machine != printer_id:
                raise ControlRefusal(
                    409,
                    {
                        "detail": "the submission targets a different printer",
                        "target_machine": job.target_machine,
                    },
                )
            if job.state != "approved" or job.verdict is None or not job.verdict.dispatch_ready:
                raise ControlRefusal(
                    409,
                    {
                        "detail": "only an approved, dispatch-ready submission can be started",
                        "state": job.state,
                    },
                )
            if job.dispatch is not None:
                raise ControlRefusal(
                    409, {"detail": "dispatch has already been attempted for this submission"}
                )

            who, verified = self._identity(actor, request.dispatched_by)
            if self.settings.require_verified_identity and not (
                verified and job.approved_by_verified
            ):
                raise ControlRefusal(
                    403,
                    {
                        "detail": "starting a print requires edge-verified identities for both "
                        "the approver and the person starting it",
                        "approver_verified": job.approved_by_verified,
                        "caller_verified": verified,
                    },
                )

            blockers = self.gate.start_print_blockers(
                monitor, busy_with_dispatch=self.busy(printer_id)
            )
            if blockers:
                raise ControlRefusal(412, {**blockers[0], "blockers": blockers})

            facts = job.facts
            plate = monitor.definition.profile.plate
            missing = []
            if facts is None or not facts.sliced:
                missing.append("sliced plate gcode")
            if facts is None or facts.plate_index is None:
                missing.append("plate index")
            if facts is None or facts.bed_type is None:
                missing.append("recognised build plate type")
            if missing or facts is None or plate is None:
                raise ControlRefusal(
                    412,
                    {
                        "detail": "the artifact lacks what a start command needs (a sliced .3mf)",
                        "missing": missing,
                    },
                )
            if facts.bed_type != plate:
                raise ControlRefusal(
                    412,
                    {
                        "detail": f"the model was sliced for "
                        f"{facts.bed_type_label or facts.bed_type} but this printer has "
                        f"{plate_label(plate)}",
                        "artifact_bed_type": facts.bed_type,
                        "machine_plate": plate,
                    },
                )

            profile = monitor.profile()
            verdict = validate_model(
                submission_id=job.submission_id,
                facts=facts,
                profile=profile,
                requested_material=job.material,
            )
            if verdict.verdict != "pass":
                raise ControlRefusal(
                    412,
                    {
                        "detail": "revalidation against the printer's current state failed",
                        "reasons": verdict.reasons,
                    },
                )
            mapping = check_tray_mapping(request.ams_mapping, facts, profile)
            plate_check = self._plate_check(
                request, control=control, printer_id=printer_id, who=who, verified=verified
            )

            remote_name = remote_filename(job)
            record = DispatchRecord(
                dispatched_by=who,
                dispatched_by_verified=verified,
                requested_at=datetime.now(UTC),
                plate_check=plate_check,
                ams_mapping=mapping,
                bed_type=plate,
                plate_index=facts.plate_index,  # type: ignore[arg-type]
                remote_filename=remote_name,
            )
            try:
                job = await self._store.begin_dispatch(job, record)
            except InvalidTransition as exc:
                raise ControlRefusal(409, {"detail": str(exc)}) from exc
            control.active_job = job.submission_id
            if request.plate_check_method == "printer_camera" and request.snapshot_token:
                await self._keep_snapshot(request.snapshot_token, job)

            path = self._store.artifact_path(job)
            try:
                upload = await asyncio.to_thread(backend.upload_file, path, remote_name)
            except PrinterCommandError as exc:
                control.active_job = None
                return await self._store.update_dispatch(
                    job, to_state="failed", note=f"upload failed: {exc}; nothing was started"
                )
            job = await self._store.update_dispatch(job, upload_verified_bytes=upload.byte_size)

            command = StartPrintCommand(
                remote_name=remote_name,
                plate_index=record.plate_index,
                bed_type=plate,
                use_ams=True,
                ams_mapping=tuple(a.printer_index for a in mapping),
                bed_leveling=self.settings.bed_leveling,
                flow_calibration=self.settings.flow_calibration,
                vibration_calibration=self.settings.vibration_calibration,
                timelapse=self.settings.timelapse,
            )
            try:
                await asyncio.to_thread(backend.start_print, command)
            except PrinterCommandError as exc:
                control.active_job = None
                note = (
                    f"start command not confirmed by the broker: {exc}; the file is on the "
                    "printer but no start was confirmed -- check the printer"
                )
                return await self._store.update_dispatch(
                    job, to_state="failed", note=note, uncertainty=note
                )
            job = await self._store.update_dispatch(job, command_published_at=datetime.now(UTC))

        return await self._await_running(job, monitor, backend, control)

    async def _await_running(
        self,
        job: SubmissionJob,
        monitor: PrinterMonitor,
        backend: PrinterBackend,
        control: _PrinterControl,
    ) -> SubmissionJob:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.confirm_timeout_s
        stem = Path(job.dispatch.remote_filename).stem if job.dispatch else ""
        while loop.time() < deadline:
            await asyncio.sleep(self._poll_s)
            if monitor.status().activity != "running":
                continue
            name = await asyncio.to_thread(backend.current_job_file)
            note = "printer reports the job running"
            if not (stem and name and stem in name):
                note += (
                    " (the printer's job name did not confirm which file; "
                    "matched on state change)"
                )
            job = await self._store.update_dispatch(
                job, to_state="running", note=note, observed_running_at=datetime.now(UTC)
            )
            control.watcher = asyncio.create_task(self._watch_running(job.submission_id))
            return job
        control.active_job = None
        note = (
            "the start command was delivered but the printer did not report the job running "
            f"within {int(self.settings.confirm_timeout_s)} s; its real state is unknown -- "
            "look at the printer before doing anything else"
        )
        return await self._store.update_dispatch(
            job, to_state="failed", note=note, uncertainty=note
        )

    async def _watch_running(self, submission_id: str) -> None:
        job = self._store.get(submission_id)
        if job is None:
            return
        monitor = self._monitors[job.target_machine]
        control = self._printers[job.target_machine]
        try:
            while True:
                await asyncio.sleep(self._poll_s)
                status = monitor.status()
                if status.activity in ("running", "unknown"):
                    continue
                print_job = status.components.get("print_job")
                gcode_state = (print_job.state if print_job and print_job.state else "").upper()
                now = datetime.now(UTC)
                if gcode_state == "FINISH":
                    await self._store.update_dispatch(
                        job,
                        to_state="finished",
                        note="printer reports the job finished",
                        observed_finished_at=now,
                    )
                else:
                    note = "printer left the running state without finishing"
                    if gcode_state:
                        note += f" (state {gcode_state.lower()})"
                    if status.last_error:
                        note += f"; printer error {status.last_error.code}"
                    await self._store.update_dispatch(
                        job, to_state="failed", note=note, observed_finished_at=now
                    )
                return
        except InvalidTransition:
            logger.exception("Could not record the outcome of %s", submission_id)
        finally:
            if control.active_job == submission_id:
                control.active_job = None
            if control.watcher is asyncio.current_task():
                control.watcher = None

    # -- stop_print --------------------------------------------------------

    async def stop_print(
        self,
        printer_id: str,
        request: StopPrintRequest,
        *,
        actor: Actor,
        claim_token: str | None,
    ) -> StopPrintResponse:
        control = self._control(printer_id)
        self.require_claim(printer_id, claim_token)
        who, verified = self._identity(actor, request.stopped_by)
        if self.settings.require_verified_identity and not verified:
            raise ControlRefusal(
                403, {"detail": "stopping a print requires an edge-verified identity"}
            )
        blockers = self.gate.stop_print_blockers(self._monitors[printer_id])
        if blockers:
            raise ControlRefusal(412, {**blockers[0], "blockers": blockers})
        try:
            await asyncio.to_thread(self._backends[printer_id].stop_print)
        except PrinterCommandError as exc:
            raise ControlRefusal(502, {"detail": f"stop command not delivered: {exc}"}) from exc
        note = f"stop requested by {who}{' (verified identity)' if verified else ''}"
        if request.reason:
            note += f": {request.reason.strip()}"
        logger.info("%s on %s", note, printer_id)
        return StopPrintResponse(
            printer_id=printer_id,
            stop_requested_at=datetime.now(UTC),
            submission_id=control.active_job,
            note=note,
        )

    # -- helpers -----------------------------------------------------------

    def _identity(self, actor: Actor, supplied: str | None) -> tuple[str, bool]:
        if actor.verified and actor.user:
            return actor.user, True
        name = (supplied or "").strip()[:120]
        if not name and not self.settings.require_verified_identity:
            raise ControlRefusal(422, {"detail": "a name is required"})
        return name or "unverified caller", False

    def _plate_check(
        self,
        request: StartPrintRequest,
        *,
        control: _PrinterControl,
        printer_id: str,
        who: str,
        verified: bool,
    ) -> PlateCheck:
        now = datetime.now(UTC)
        if request.plate_check_method == "in_person":
            return PlateCheck(
                confirmed_by=who,
                confirmed_by_verified=verified,
                method="in_person",
                confirmed_at=now,
            )
        snap = self._snapshots.get(request.snapshot_token or "")
        holder = control.claims.holder()
        if (
            snap is None
            or snap.printer_id != printer_id
            or (holder is not None and snap.session_id != holder.session_id)
        ):
            raise ControlRefusal(
                412,
                {
                    "detail": "camera confirmation needs a snapshot taken under this claim",
                    "snapshot_missing": True,
                },
            )
        age = (now - snap.captured_at).total_seconds()
        if age > SNAPSHOT_MAX_AGE_S:
            raise ControlRefusal(
                412,
                {
                    "detail": "the camera snapshot is too old; take a new one",
                    "snapshot_age_s": round(age, 1),
                    "snapshot_max_age_s": SNAPSHOT_MAX_AGE_S,
                },
            )
        return PlateCheck(
            confirmed_by=who,
            confirmed_by_verified=verified,
            method="printer_camera",
            confirmed_at=now,
            snapshot_sha256=snap.sha256,
            snapshot_captured_at=snap.captured_at,
        )

    async def _keep_snapshot(self, token: str, job: SubmissionJob) -> None:
        snap = self._snapshots.pop(token, None)
        if snap is not None:
            target = self._store.root / f"{job.submission_id}.plate.jpg"
            await asyncio.to_thread(snap.path.replace, target)


def remote_filename(job: SubmissionJob) -> str:
    """A printer-side filename: ASCII, short, unique per submission."""

    stem = _REMOTE_NAME_RE.sub("_", Path(job.original_filename).stem).strip("._-")[:40]
    return f"gw_{job.submission_id[:12]}_{stem or 'job'}.3mf"


def check_tray_mapping(
    assignments: list[TrayAssignment], facts: ArtifactFacts, profile: MachineProfile
) -> list[TrayAssignment]:
    """Refuse unless every filament the plate uses maps to a tray that holds it."""

    uses = {use.filament_id: use.filament_type for use in facts.filaments}
    if not uses:
        if len(facts.filament_types) != 1:
            raise ControlRefusal(
                412,
                {
                    "detail": "the artifact's filament slots could not be determined",
                    "filament_types": list(facts.filament_types),
                },
            )
        uses = {1: facts.filament_types[0]}
    trays = {(t.ams_id, t.tray_id): t for t in profile.observed.loaded_trays}
    if not trays:
        raise ControlRefusal(
            412,
            {"detail": "no loaded AMS trays are reported, so the mapping cannot be checked"},
        )
    problems: list[str] = []
    seen: set[int] = set()
    for assignment in assignments:
        if assignment.filament_id in seen:
            problems.append(f"filament {assignment.filament_id} is mapped twice")
        seen.add(assignment.filament_id)
        needed = uses.get(assignment.filament_id)
        if needed is None:
            problems.append(f"filament {assignment.filament_id} is not used by this plate")
            continue
        where = f"AMS {assignment.ams_id} tray {assignment.tray_id}"
        tray = trays.get((assignment.ams_id, assignment.tray_id))
        if tray is None:
            problems.append(f"{where} reports no loaded filament")
            continue
        loaded = (tray.tray_type or "").strip().upper()
        if loaded != needed.strip().upper():
            problems.append(
                f"filament {assignment.filament_id} needs {needed} but {where} holds "
                f"{loaded or 'unknown'}"
            )
        temp = facts.nozzle_temperature_c
        if (
            temp is not None
            and tray.nozzle_temp_min_c is not None
            and tray.nozzle_temp_max_c is not None
            and not tray.nozzle_temp_min_c <= temp <= tray.nozzle_temp_max_c
        ):
            problems.append(
                f"the model's {temp:g} C nozzle is outside the {tray.nozzle_temp_min_c}-"
                f"{tray.nozzle_temp_max_c} C window of the spool in {where}"
            )
    unmapped = sorted(set(uses) - seen)
    if unmapped:
        problems.append(f"filament(s) {unmapped} have no tray assigned")
    if problems:
        raise ControlRefusal(
            412,
            {"detail": "the tray mapping does not match the loaded filament", "problems": problems},
        )
    return sorted(assignments, key=lambda a: a.filament_id)
