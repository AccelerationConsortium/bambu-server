"""FastAPI application for the monitoring-only Bambu printer gateway.

Two surfaces live here and they are deliberately different in kind:

* the **status surface** (``/printers/*``) is a pure read of the background
  monitor's cache -- no request on it ever reaches a printer;
* the **submission surface** (``/submissions``, ``/printers/{id}/queue``) accepts
  print jobs, validates them against the target machine's profile, and queues
  them. It is read-and-analysis too: intake writes to the gateway's own disk,
  and the one step that would touch a printer -- dispatch -- is not implemented
  here (see :func:`bambu_server.submissions.dispatch`).

There are no ``/control/*`` routes, and the service still issues no printer
commands.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Path, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import __version__
from .artifacts import ARTIFACT_EXTENSIONS
from .backend import BambuLabsBackend, PrinterBackend
from .config import (
    PrinterCredentials,
    PrinterDefinition,
    Settings,
    load_settings,
    resolve_credentials,
)
from .models import (
    PROTOCOL_VERSION,
    ComponentStatus,
    EquipmentStatus,
    GatewayInfo,
    HealthResponse,
    PrinterSummary,
    ProbeResponse,
)
from .monitor import PrinterMonitor
from .profiles import MachineProfile
from .queueing import QueueView, build_queue_view
from .submissions import (
    ArtifactTooLarge,
    InvalidTransition,
    JobState,
    SubmissionError,
    SubmissionJob,
    SubmissionStore,
    run_validation,
)

BackendFactory = Callable[[PrinterDefinition, PrinterCredentials], PrinterBackend]

#: Read size for streaming an upload to disk.
_UPLOAD_CHUNK_BYTES = 1 << 20

#: Leading bytes an artifact must start with, keyed by kind. A ``.3mf`` is a
#: zip container; anything else under that name is a malformed submission and
#: is refused at intake rather than carried through validation.
_MAGIC_PREFIXES: dict[str, bytes] = {"3mf": b"PK\x03\x04"}


class ApprovalRequest(BaseModel):
    """Sign-off recorded against a queued submission.

    ``approved_by`` is an opaque identifier, **not** an authenticated identity:
    this service has no login, and access is gated at the network layer. It is
    recorded in the job's history so the decision is attributable once a real
    identity provider is wired in.
    """

    approved_by: str = Field(min_length=1, max_length=120)


class CancellationRequest(BaseModel):
    """Withdrawal of a waiting submission.

    ``cancelled_by`` is opaque, on the same terms as ``approved_by``. ``reason``
    is free text kept in the job's history so a withdrawal is explicable later.
    """

    cancelled_by: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=500)


def create_app(
    *,
    settings: Settings | None = None,
    backend_factory: BackendFactory = BambuLabsBackend,
) -> FastAPI:
    monitors: dict[str, PrinterMonitor] = {}
    # One-slot holder rather than a module global: `create_app` may be called
    # more than once in a process (tests do), and each app owns its own store.
    stores: dict[str, SubmissionStore] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or load_settings()
        app.state.settings = active_settings
        store = SubmissionStore(active_settings.submissions)
        await asyncio.to_thread(store.load)
        stores["default"] = store
        app.state.submissions = store
        for definition in active_settings.printers:
            credentials = resolve_credentials(definition)
            monitor = PrinterMonitor(
                definition,
                backend_factory(definition, credentials),
                poll_interval_seconds=active_settings.poll_interval_seconds,
                stale_after_seconds=active_settings.stale_after_seconds,
            )
            monitors[definition.id] = monitor
        try:
            for monitor in monitors.values():
                await monitor.start()
            yield
        finally:
            for monitor in reversed(list(monitors.values())):
                await monitor.stop()
            monitors.clear()
            stores.clear()

    app = FastAPI(
        title="AC Bambu Printer Gateway",
        version=__version__,
        description=(
            "Monitoring-only MQTT gateway for Bambu Lab printers. Each configured "
            "printer is exposed through the AC lab equipment status spec v1.2."
        ),
        lifespan=lifespan,
    )

    configured_origins = settings.cors_origins if settings else ["http://localhost:8000"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configured_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def get_monitor(
        printer_id: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]*$")],
    ) -> PrinterMonitor:
        monitor = monitors.get(printer_id)
        if monitor is None:
            raise HTTPException(status_code=404, detail="printer not configured")
        return monitor

    def get_store() -> SubmissionStore:
        store = stores.get("default")
        if store is None:  # pragma: no cover - only outside the app lifespan
            raise HTTPException(status_code=503, detail="submission store unavailable")
        return store

    @app.get("/", response_model=GatewayInfo, tags=["gateway"])
    async def gateway_info() -> GatewayInfo:
        return GatewayInfo(
            service="ac-bambu-server",
            version=__version__,
            printer_count=len(monitors),
        )

    @app.get("/health", response_model=HealthResponse, tags=["gateway"])
    async def gateway_health() -> HealthResponse:
        return HealthResponse()

    @app.get("/status", response_model=EquipmentStatus, tags=["gateway"])
    async def gateway_status() -> EquipmentStatus:
        printer_statuses = {
            printer_id: monitor.status() for printer_id, monitor in monitors.items()
        }
        components: dict[str, ComponentStatus] = {}
        for printer_id, status in printer_statuses.items():
            mqtt = status.components.get("mqtt")
            components[printer_id] = ComponentStatus(
                connected=bool(mqtt and mqtt.connected),
                state=status.equipment_status,
                message=status.message,
                last_event_at=mqtt.last_event_at if mqtt else None,
            )
        connected_count = sum(component.connected for component in components.values())
        if connected_count == len(components):
            state = "ready"
            message = "All printer monitors are connected"
        elif connected_count:
            state = "degraded"
            message = "Some printer monitors are disconnected"
        else:
            state = "unknown"
            message = "No printer monitors are connected"

        return EquipmentStatus(
            equipment_id="bambu_gateway",
            equipment_name="Bambu Gateway",
            equipment_kind="other",
            equipment_version=__version__,
            equipment_status=state,
            message=message,
            device_time=datetime.now(UTC),
            components=components,
            allowed_actions=[],
            details={"monitoring_only": True, "printer_count": len(monitors)},
        )

    @app.get("/printers", response_model=list[PrinterSummary], tags=["gateway"])
    async def list_printers() -> list[PrinterSummary]:
        return [
            PrinterSummary(
                id=monitor.definition.id,
                name=monitor.definition.name,
                model=monitor.definition.model,
                status_path=f"/printers/{monitor.definition.id}/status",
            )
            for monitor in monitors.values()
        ]

    @app.get("/printers/{printer_id}/", response_model=ProbeResponse, tags=["printers"])
    async def printer_probe(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
    ) -> ProbeResponse:
        return ProbeResponse(
            equipment_id=monitor.definition.id,
            equipment_name=monitor.definition.name,
            protocol_version=PROTOCOL_VERSION,
        )

    @app.get(
        "/printers/{printer_id}/health",
        response_model=HealthResponse,
        tags=["printers"],
    )
    async def printer_health(
        _monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
    ) -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/printers/{printer_id}/status",
        response_model=EquipmentStatus,
        tags=["printers"],
    )
    async def printer_status(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
    ) -> EquipmentStatus:
        return monitor.status()

    @app.get(
        "/printers/{printer_id}/profile",
        response_model=MachineProfile,
        tags=["submissions"],
    )
    async def printer_profile(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
    ) -> MachineProfile:
        """The machine a submitter targets, and what a model is checked against.

        Merges the operator-declared profile with what the printer currently
        reports. Reads the monitor's cache only.
        """

        return monitor.profile()

    @app.get(
        "/printers/{printer_id}/queue",
        response_model=QueueView,
        tags=["submissions"],
    )
    async def printer_queue(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        store: Annotated[SubmissionStore, Depends(get_store)],
    ) -> QueueView:
        """Running job and waiting submissions for one machine, with finish times.

        Side-effect free: it neither polls the printer nor advances the queue.
        """

        return build_queue_view(
            machine=monitor.definition.id,
            status=monitor.status(),
            jobs=store.queue_for(monitor.definition.id),
        )

    @app.post(
        "/submissions",
        response_model=SubmissionJob,
        status_code=201,
        tags=["submissions"],
    )
    async def create_submission(
        store: Annotated[SubmissionStore, Depends(get_store)],
        file: Annotated[UploadFile, File(description="A .3mf or .gcode artifact")],
        target_machine: Annotated[str, Form(max_length=120)],
        requested_by: Annotated[str, Form(max_length=120)],
        material: Annotated[str | None, Form(max_length=60)] = None,
    ) -> SubmissionJob:
        """Accept a print artifact, validate it, and queue it if it passes.

        Performs no printer I/O. Validation runs inline so the caller gets the
        per-check verdict in the response; the file read happens on a worker
        thread so a large artifact does not stall the status poll loop.
        """

        monitor = monitors.get(target_machine)
        if monitor is None:
            raise HTTPException(status_code=404, detail="unknown target machine")

        extension = PurePosixPath(file.filename or "").suffix.lower()
        kind = ARTIFACT_EXTENSIONS.get(extension)
        if kind is None:
            supported = ", ".join(sorted(ARTIFACT_EXTENSIONS))
            raise HTTPException(
                status_code=400, detail=f"artifact must be one of: {supported}"
            )

        magic = _MAGIC_PREFIXES.get(kind)
        if magic is not None:
            head = await file.read(len(magic))
            await file.seek(0)
            if head != magic:
                raise HTTPException(
                    status_code=400,
                    detail=f"the uploaded file is not a valid {kind} container",
                )

        async def chunks() -> AsyncIterator[bytes]:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk

        try:
            job = await store.accept(
                chunks=chunks(),
                extension=extension,
                target_machine=target_machine,
                requested_by=requested_by,
                material=material,
                original_filename=file.filename or "",
            )
        except ArtifactTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except SubmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return await run_validation(store, job, monitor.profile())

    @app.get("/submissions", response_model=list[SubmissionJob], tags=["submissions"])
    async def list_submissions(
        store: Annotated[SubmissionStore, Depends(get_store)],
        machine: Annotated[str | None, Query(max_length=120)] = None,
        state: Annotated[JobState | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[SubmissionJob]:
        return store.list(machine=machine, state=state)[:limit]

    @app.get(
        "/submissions/{submission_id}",
        response_model=SubmissionJob,
        tags=["submissions"],
    )
    async def read_submission(
        store: Annotated[SubmissionStore, Depends(get_store)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> SubmissionJob:
        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        return job

    @app.post(
        "/submissions/{submission_id}/approve",
        response_model=SubmissionJob,
        tags=["submissions"],
    )
    async def approve_submission(
        store: Annotated[SubmissionStore, Depends(get_store)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
        approval: ApprovalRequest,
    ) -> SubmissionJob:
        """Record sign-off on a queued submission.

        This is the approval gate and nothing more: it moves no hardware, starts
        no print, and reaches no printer. It marks the job ``approved`` and sets
        ``verdict.dispatch_ready``, which a future dispatch step would require.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        try:
            return await store.approve(job, approved_by=approval.approved_by)
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/submissions/{submission_id}/cancel",
        response_model=SubmissionJob,
        tags=["submissions"],
    )
    async def cancel_submission(
        store: Annotated[SubmissionStore, Depends(get_store)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
        cancellation: CancellationRequest,
    ) -> SubmissionJob:
        """Withdraw a waiting submission from its machine's queue.

        A queue operation, not an abort: it reaches no printer and is refused
        for anything past the queue. Deletes the stored artifact and keeps the
        job record, so the withdrawal stays auditable.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        try:
            return await store.cancel(
                job,
                cancelled_by=cancellation.cancelled_by,
                reason=cancellation.reason,
            )
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SubmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


def application_factory() -> FastAPI:
    """Load local configuration before constructing middleware and monitors."""

    return create_app(settings=load_settings())


# Import-friendly fallback for introspection and tests. Production entrypoints
# use ``application_factory`` so file-based CORS configuration is applied.
app = create_app()
