"""FastAPI application for the Bambu printer gateway.

Three surfaces live here and they are deliberately different in kind:

* the **status surface** (``/printers/*``) is a pure read of the background
  monitor's cache -- no request on it ever reaches a printer;
* the **submission surface** (``/submissions``, ``/printers/{id}/queue``) accepts
  print jobs (a sliced ``.3mf``/``.gcode``, or an ``.stl`` the gateway slices),
  validates them against the target machine's profile, and queues them. It
  writes to the gateway's own disk and never reaches a printer;
* the **control surface** (``/printers/{id}/control/*``) exists only when the
  local config enables dispatch. Every route on it needs a live claim; it is
  the only place a request can cause printer I/O, and it does so only through
  :mod:`bambu_server.dispatch`.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path as PathLib
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sdl_lab_contract.claims import ClaimRejection, ClaimRequest, ClaimResponse

from . import __version__
from .artifacts import ARTIFACT_EXTENSIONS
from .backend import BambuLabsBackend, PrinterBackend
from .claims import ClaimConflict, ClaimLost
from .config import (
    PrinterCredentials,
    PrinterDefinition,
    Settings,
    load_settings,
    resolve_credentials,
    resolve_edge_secret,
)
from .dispatch import (
    ControlRefusal,
    Dispatcher,
    LightRequest,
    LightResponse,
    SnapshotResponse,
    StartPrintRequest,
    StopPrintRequest,
    StopPrintResponse,
)
from .identity import Actor, actor_for, resolve_actor
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
from .slicer import BambuStudioSlicer, SliceRequest, SlicerError, SlicerUnavailable
from .submissions import (
    ArtifactTooLarge,
    InvalidTransition,
    JobState,
    SliceProvenance,
    SubmissionError,
    SubmissionJob,
    SubmissionStore,
    run_validation,
    safe_display_name,
)

#: Header carrying the claim token on control routes (STATUS_SPEC §5).
CLAIM_HEADER = "X-Claim-Token"

logger = logging.getLogger(__name__)

BackendFactory = Callable[[PrinterDefinition, PrinterCredentials], PrinterBackend]

#: Read size for streaming an upload to disk.
_UPLOAD_CHUNK_BYTES = 1 << 20

#: The submission page. One self-contained file with no build step and no
#: external resources, served from the same origin as the API it calls, so a
#: browser needs neither a bundler nor a CORS exemption to use it.
_UI_PAGE = PathLib(__file__).parent / "static" / "index.html"

#: Leading bytes an artifact must start with, keyed by kind. A ``.3mf`` is a
#: zip container; anything else under that name is a malformed submission and
#: is refused at intake rather than carried through validation.
_MAGIC_PREFIXES: dict[str, bytes] = {"3mf": b"PK\x03\x04"}


class ApprovalRequest(BaseModel):
    """Sign-off recorded against a queued submission.

    When the request arrives through the lab's authenticating edge, the
    signed-in identity is used and this field is ignored — a signed-in person
    must not be able to file a decision under someone else's name. Without a
    verified identity it is an opaque label, and the job records which of the
    two it got.
    """

    #: Optional: ignored when the edge supplies a verified identity, and
    #: required only when it does not.
    approved_by: str | None = Field(default=None, min_length=1, max_length=120)


class CancellationRequest(BaseModel):
    """Withdrawal of a waiting submission.

    ``cancelled_by`` is opaque, on the same terms as ``approved_by``. ``reason``
    is free text kept in the job's history so a withdrawal is explicable later.
    """

    cancelled_by: str | None = Field(default=None, min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=500)


class Whoami(BaseModel):
    """Who the service thinks you are, for the page to render honestly.

    The page shows a name field when nobody is verified and the signed-in user
    when someone is; without this it would have to guess how it was deployed.
    """

    user: str | None = None
    role: str | None = None
    verified: bool = False
    #: True when this deployment is capable of verifying an identity at all
    #: (a secret is configured). Distinguishes "not signed in" from "this
    #: service cannot tell who you are".
    identity_available: bool = False


def create_app(
    *,
    settings: Settings | None = None,
    backend_factory: BackendFactory = BambuLabsBackend,
) -> FastAPI:
    monitors: dict[str, PrinterMonitor] = {}
    # One-slot holder rather than a module global: `create_app` may be called
    # more than once in a process (tests do), and each app owns its own store.
    stores: dict[str, SubmissionStore] = {}
    # Resolved once at startup, not per request: it is a process-level
    # credential, and re-reading the environment per call would let a later
    # change silently alter who the service trusts mid-run.
    secrets: dict[str, str | None] = {"edge": None}
    # Control-plane objects, present only while the app is running. A one-slot
    # dict for the same reason as `stores`.
    dispatchers: dict[str, Dispatcher] = {}
    slicers: dict[str, BambuStudioSlicer] = {}
    control_enabled = bool(settings and settings.dispatch.enabled)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or load_settings()
        app.state.settings = active_settings
        secrets["edge"] = resolve_edge_secret()
        store = SubmissionStore(active_settings.submissions)
        await asyncio.to_thread(store.load)
        stores["default"] = store
        app.state.submissions = store
        backends: dict[str, PrinterBackend] = {}
        for definition in active_settings.printers:
            credentials = resolve_credentials(definition)
            backend = backend_factory(definition, credentials)
            backends[definition.id] = backend
            monitor = PrinterMonitor(
                definition,
                backend,
                poll_interval_seconds=active_settings.poll_interval_seconds,
                stale_after_seconds=active_settings.stale_after_seconds,
            )
            monitors[definition.id] = monitor
        dispatcher = Dispatcher(
            settings=active_settings.dispatch,
            store=store,
            monitors=monitors,
            backends=backends,
            poll_interval_s=active_settings.poll_interval_seconds,
        )
        dispatchers["default"] = dispatcher
        slicers["default"] = BambuStudioSlicer(active_settings.slicer)
        try:
            for monitor in monitors.values():
                await monitor.start()
            if active_settings.dispatch.enabled:
                await dispatcher.start()
            yield
        finally:
            await dispatcher.stop()
            for monitor in reversed(list(monitors.values())):
                await monitor.stop()
            monitors.clear()
            stores.clear()
            dispatchers.clear()
            slicers.clear()

    app = FastAPI(
        title="AC Bambu Printer Gateway",
        version=__version__,
        description=(
            "Monitoring-only MQTT gateway for Bambu Lab printers. Each configured "
            "printer is exposed through the AC lab equipment status spec v1.2. "
            "Profiles expose typed AMS inventory, color-label provenance, and remaining "
            "filament estimates. Submission, validation, queuing and approval only store "
            "gateway records; dispatch is unavailable. Use the authenticated /bambu/ "
            "edge path for browser access. No credentials or raw MQTT payloads are exposed."
        ),
        docs_url=None,
        servers=[{"url": ".", "description": "Same-origin gateway path, including any authenticated edge prefix"}],
        lifespan=lifespan,
    )

    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/docs/", include_in_schema=False, response_class=HTMLResponse)
    async def api_docs(request: Request) -> HTMLResponse:
        # Relative URLs retain the browser's /bambu/ prefix, stripped by Caddy.
        return get_swagger_ui_html(
            openapi_url="../openapi.json" if request.url.path.endswith("/") else "./openapi.json",
            title="Bambu Gateway API",
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

    def get_actor(request: Request) -> Actor:
        """The caller, as far as the trusted edge will vouch for them."""

        return resolve_actor(request.headers, edge_secret=secrets["edge"])

    def get_store() -> SubmissionStore:
        store = stores.get("default")
        if store is None:  # pragma: no cover - only outside the app lifespan
            raise HTTPException(status_code=503, detail="submission store unavailable")
        return store

    def get_dispatcher() -> Dispatcher:
        dispatcher = dispatchers.get("default")
        if dispatcher is None:  # pragma: no cover - only outside the app lifespan
            raise HTTPException(status_code=503, detail="control plane unavailable")
        return dispatcher

    def get_slicer() -> BambuStudioSlicer:
        slicer = slicers.get("default")
        if slicer is None:  # pragma: no cover - only outside the app lifespan
            raise HTTPException(status_code=503, detail="slicer unavailable")
        return slicer

    @app.get("/", response_model=GatewayInfo, tags=["gateway"])
    async def gateway_info() -> GatewayInfo:
        return GatewayInfo(
            service="ac-bambu-server",
            version=__version__,
            mode="control" if control_enabled else "monitoring_only",
            printer_count=len(monitors),
        )

    # Registered at both spellings on purpose. Behind an edge path prefix the
    # canonical URL is `<prefix>/ui/`, and serving only `/ui` would make
    # Starlette answer the trailing-slash form with a redirect to `/ui` --
    # whose Location drops the prefix, landing the visitor on the dashboard.
    # (The same trap the xArm's /web/ route documents in the edge config.)
    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False, tags=["gateway"])
    @app.get("/ui/", response_class=HTMLResponse, include_in_schema=False, tags=["gateway"])
    async def submission_ui() -> HTMLResponse:
        """The operator/submitter page.

        Deliberately a single static file: it calls the same public endpoints
        any other client would, holds no state of its own, and cannot do
        anything the API would refuse.
        """

        return HTMLResponse(_UI_PAGE.read_text(encoding="utf-8"))

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
            details={"monitoring_only": not control_enabled, "printer_count": len(monitors)},
        )

    @app.get("/whoami", response_model=Whoami, tags=["gateway"])
    async def whoami(actor: Annotated[Actor, Depends(get_actor)]) -> Whoami:
        """Report the caller's verified identity, if the edge supplied one.

        Never echoes the shared secret, and says nothing a caller did not
        already present.
        """

        return Whoami(
            user=actor.user,
            role=actor.role,
            verified=actor.verified,
            identity_available=secrets["edge"] is not None,
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
        """Cached STATUS_SPEC envelope. details.ams_trays includes unit/slot IDs,
        reported color, tray_color_name, tray_color_source, and remaining_percent.
        details.ams_unit_ids includes empty units. Use the profile endpoint for
        typed inventory schemas and freshness-filtered observations.

        With dispatch enabled, ``allowed_actions`` is computed by the same gate
        the control routes refuse with (STATUS_SPEC §6.2) and
        ``details.claimed_by`` names the current claim holder.
        """
        status = monitor.status()
        if not control_enabled:
            return status
        dispatcher = get_dispatcher()
        printer_id = monitor.definition.id
        holder = dispatcher.claims(printer_id).holder()
        details = {
            **status.details,
            "monitoring_only": False,
            "claimed_by": holder.model_dump(mode="json") if holder else None,
            "build_plate": monitor.definition.profile.plate,
        }
        return status.model_copy(
            update={"allowed_actions": dispatcher.allowed_actions(printer_id), "details": details}
        )

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
        file: Annotated[
            UploadFile,
            File(
                description="A sliced .3mf or .gcode, or an .stl the gateway slices "
                "(when slicing is enabled for the target; requires material)"
            ),
        ],
        actor: Annotated[Actor, Depends(get_actor)],
        target_machine: Annotated[str, Form(max_length=120)],
        requested_by: Annotated[str | None, Form(max_length=120)] = None,
        material: Annotated[str | None, Form(max_length=60)] = None,
    ) -> SubmissionJob:
        """Accept a print artifact, validate it, and queue it if it passes.

        Performs no printer I/O. Validation runs inline so the caller gets the
        per-check verdict in the response; the file read happens on a worker
        thread so a large artifact does not stall the status poll loop.
        """

        # A verified identity wins over the form field: a signed-in person must
        # not be able to submit under someone else's name.
        try:
            owner, owner_verified = actor_for(actor, requested_by)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="requested_by is required") from exc

        monitor = monitors.get(target_machine)
        if monitor is None:
            raise HTTPException(status_code=404, detail="unknown target machine")

        extension = PurePosixPath(file.filename or "").suffix.lower()
        if extension == ".stl":
            return await _slice_and_accept(
                store=store,
                slicer=get_slicer(),
                monitor=monitor,
                file=file,
                owner=owner,
                owner_verified=owner_verified,
                material=material,
            )
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
                requested_by=owner,
                requested_by_verified=owner_verified,
                material=material,
                original_filename=file.filename or "",
            )
        except ArtifactTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except SubmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return await run_validation(store, job, monitor.profile())

    async def _slice_and_accept(
        *,
        store: SubmissionStore,
        slicer: BambuStudioSlicer,
        monitor: PrinterMonitor,
        file: UploadFile,
        owner: str,
        owner_verified: bool,
        material: str | None,
    ) -> SubmissionJob:
        """Slice an uploaded mesh for the target machine, then intake the 3mf.

        The mesh and the slicer's scratch files live in a per-request directory
        under the slicer's work dir and are removed afterwards; what survives
        is the sliced 3mf (as an ordinary submission) and its provenance.
        """

        printer_id = monitor.definition.id
        if not slicer.accepts(printer_id):
            raise HTTPException(
                status_code=400,
                detail=f"this gateway does not slice .stl files for {printer_id}; "
                "upload a sliced .3mf or .gcode",
            )
        if not material or not material.strip():
            raise HTTPException(
                status_code=422,
                detail="material is required to slice an .stl (one of: "
                + ", ".join(slicer.materials()) + ")",
            )
        settings_ = slicer.settings
        job_dir = settings_.work_dir / uuid.uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            mesh = job_dir / "model.stl"
            digest = hashlib.sha256()
            size = 0
            with mesh.open("wb") as handle:
                while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > settings_.max_stl_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"mesh exceeds the {settings_.max_stl_bytes} byte limit",
                        )
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="the uploaded mesh is empty")

            request = SliceRequest(
                printer_id=printer_id,
                material=material,
                nozzle_type=monitor.profile().nozzle_type,
                plate=monitor.definition.profile.plate,
            )
            try:
                machine, process, filament = slicer.resolve_presets(request)
                result = await slicer.slice(mesh, request, job_dir=job_dir)
            except SlicerUnavailable as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except SlicerError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            source_name = safe_display_name(file.filename or "model.stl")
            provenance = SliceProvenance(
                source_filename=source_name,
                source_sha256=digest.hexdigest(),
                source_byte_size=size,
                slicer_version=result.slicer_version,
                machine_preset=machine.stem,
                process_preset=process.stem,
                filament_preset=filament.stem,
                warnings=list(result.warnings),
            )

            async def sliced_chunks() -> AsyncIterator[bytes]:
                with result.artifact.open("rb") as artifact:
                    while chunk := await asyncio.to_thread(artifact.read, _UPLOAD_CHUNK_BYTES):
                        yield chunk

            try:
                job = await store.accept(
                    chunks=sliced_chunks(),
                    extension=".3mf",
                    target_machine=printer_id,
                    requested_by=owner,
                    requested_by_verified=owner_verified,
                    material=material,
                    original_filename=f"{PurePosixPath(source_name).stem}.3mf",
                    provenance=provenance,
                )
            except ArtifactTooLarge as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc
            except SubmissionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await asyncio.to_thread(slicer.discard, job_dir)
        return await run_validation(store, job, monitor.profile())

    @app.get(
        "/submissions/{submission_id}/plate.jpg",
        response_class=FileResponse,
        tags=["submissions"],
    )
    async def submission_plate_snapshot(
        store: Annotated[SubmissionStore, Depends(get_store)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> FileResponse:
        """The camera frame a person confirmed the empty plate against.

        Evidence for the dispatch record, read from the gateway's own disk.
        404 when the plate was confirmed in person or no dispatch happened.
        """

        job = store.get(submission_id)
        path = store.root / f"{submission_id}.plate.jpg"
        if job is None or not path.is_file():
            raise HTTPException(status_code=404, detail="no plate snapshot for this submission")
        return FileResponse(path, media_type="image/jpeg")

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
        actor: Annotated[Actor, Depends(get_actor)],
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
            who, verified = actor_for(actor, approval.approved_by)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="approved_by is required") from exc
        try:
            return await store.approve(job, approved_by=who, verified=verified)
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/submissions/{submission_id}/cancel",
        response_model=SubmissionJob,
        tags=["submissions"],
    )
    async def cancel_submission(
        store: Annotated[SubmissionStore, Depends(get_store)],
        actor: Annotated[Actor, Depends(get_actor)],
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
            who, verified = actor_for(actor, cancellation.cancelled_by)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="cancelled_by is required") from exc
        try:
            return await store.cancel(
                job,
                cancelled_by=who,
                reason=cancellation.reason,
                verified=verified,
            )
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SubmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete(
        "/submissions/{submission_id}",
        status_code=204,
        tags=["submissions"],
    )
    async def delete_submission(
        store: Annotated[SubmissionStore, Depends(get_store)],
        actor: Annotated[Actor, Depends(get_actor)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> None:
        """Delete a finished job's record and artifact.

        Retention housekeeping. Only a terminal job can be deleted: withdrawing
        one that is still waiting is `cancel`, which leaves a record of the
        decision, and deleting it instead would erase that along with the job.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        try:
            forgotten = await store.forget(job)
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.info(
            "Deleted submission %s (%s) on behalf of %s%s",
            forgotten.submission_id,
            forgotten.state,
            actor.user or "an unidentified caller",
            " (verified)" if actor.verified else "",
        )

    if control_enabled:
        _register_control_routes(app, get_monitor, get_actor, get_dispatcher)

    return app


def _refusal(exc: ControlRefusal) -> JSONResponse:
    headers = {}
    retry = exc.body.get("retry_after_s")
    if isinstance(retry, (int, float)):
        headers["Retry-After"] = str(int(retry))
    return JSONResponse(status_code=exc.status, content=exc.body, headers=headers)


def _register_control_routes(
    app: FastAPI,
    get_monitor: Callable[..., PrinterMonitor],
    get_actor: Callable[..., Actor],
    get_dispatcher: Callable[[], Dispatcher],
) -> None:
    """STATUS_SPEC §5 claims plus the three control verbs, per printer.

    Only called when the local config enables dispatch, so a deployment that
    has not opted in exposes no ``/control/`` path at all.
    """

    control_responses = {
        412: {"description": "A precondition is not met; the body names which."},
        423: {"description": "No live claim token was presented."},
    }

    @app.post(
        "/printers/{printer_id}/control/claim",
        response_model=ClaimResponse,
        responses={409: {"model": ClaimRejection}},
        tags=["control"],
    )
    async def control_claim(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        actor: Annotated[Actor, Depends(get_actor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        request: ClaimRequest,
    ) -> ClaimResponse | JSONResponse:
        """Acquire this printer's claim. A verified identity replaces ``owner``."""

        if actor.verified and actor.user:
            request = request.model_copy(update={"owner": actor.user})
        try:
            return dispatcher.claims(monitor.definition.id).claim(request)
        except ClaimConflict as exc:
            retry = max(0.0, (exc.holder.expires_at - datetime.now(UTC)).total_seconds())
            body = ClaimRejection(
                detail="this printer is claimed by another session",
                claimed_by=exc.holder,
                retry_after_s=round(retry, 1),
            )
            return JSONResponse(
                status_code=409,
                content=body.model_dump(mode="json"),
                headers={"Retry-After": str(int(retry) + 1)},
            )

    @app.post(
        "/printers/{printer_id}/control/heartbeat",
        status_code=200,
        tags=["control"],
    )
    async def control_heartbeat(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        claim_token: Annotated[str | None, Header(alias=CLAIM_HEADER)] = None,
    ) -> dict[str, datetime]:
        try:
            expires = dispatcher.claims(monitor.definition.id).heartbeat(claim_token)
        except ClaimLost as exc:
            raise HTTPException(status_code=401, detail="claim lost") from exc
        return {"expires_at": expires}

    @app.post(
        "/printers/{printer_id}/control/release",
        status_code=204,
        tags=["control"],
    )
    async def control_release(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        claim_token: Annotated[str | None, Header(alias=CLAIM_HEADER)] = None,
    ) -> Response:
        dispatcher.claims(monitor.definition.id).release(claim_token)
        return Response(status_code=204)

    @app.post(
        "/printers/{printer_id}/control/snapshot",
        response_model=SnapshotResponse,
        responses={423: control_responses[423], 502: {"description": "Camera unreachable."}},
        tags=["control"],
    )
    async def control_snapshot(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        claim_token: Annotated[str | None, Header(alias=CLAIM_HEADER)] = None,
    ) -> SnapshotResponse | JSONResponse:
        """One frame from the printer's chamber camera, for the plate check.

        A POST under the claim because it opens a connection to the printer;
        nothing on a plain page view ever fetches a frame.
        """

        try:
            return await dispatcher.snapshot(monitor.definition.id, claim_token=claim_token)
        except ControlRefusal as exc:
            return _refusal(exc)

    @app.post(
        "/printers/{printer_id}/control/start_print",
        response_model=SubmissionJob,
        responses={409: {"description": "Job state conflict."}, **control_responses},
        tags=["control"],
    )
    async def control_start_print(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        actor: Annotated[Actor, Depends(get_actor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        request: StartPrintRequest,
        claim_token: Annotated[str | None, Header(alias=CLAIM_HEADER)] = None,
    ) -> SubmissionJob | JSONResponse:
        """Upload an approved job to the printer and start it.

        Requires the claim, a person's statement that the build plate is
        empty, an explicit tray for every filament, and every gate in
        :mod:`bambu_server.dispatch`. Returns once the printer reports the job
        running, or with the job ``failed`` and the uncertainty recorded.
        """

        try:
            return await dispatcher.start_print(
                monitor.definition.id, request, actor=actor, claim_token=claim_token
            )
        except ControlRefusal as exc:
            return _refusal(exc)

    @app.post(
        "/printers/{printer_id}/control/light",
        response_model=LightResponse,
        responses=control_responses,
        tags=["control"],
    )
    async def control_light(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        request: LightRequest,
        claim_token: Annotated[str | None, Header(alias=CLAIM_HEADER)] = None,
    ) -> LightResponse | JSONResponse:
        """Switch the chamber light, e.g. to see the plate in a snapshot. No motion."""

        try:
            return await dispatcher.light(
                monitor.definition.id, request, claim_token=claim_token
            )
        except ControlRefusal as exc:
            return _refusal(exc)

    @app.post(
        "/printers/{printer_id}/control/stop_print",
        response_model=StopPrintResponse,
        responses=control_responses,
        tags=["control"],
    )
    async def control_stop_print(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        actor: Annotated[Actor, Depends(get_actor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        request: StopPrintRequest,
        claim_token: Annotated[str | None, Header(alias=CLAIM_HEADER)] = None,
    ) -> StopPrintResponse | JSONResponse:
        """Stop the print in flight. The job's final state comes from the printer."""

        try:
            return await dispatcher.stop_print(
                monitor.definition.id, request, actor=actor, claim_token=claim_token
            )
        except ControlRefusal as exc:
            return _refusal(exc)


def application_factory() -> FastAPI:
    """Load local configuration before constructing middleware and monitors."""

    return create_app(settings=load_settings())


# Import-friendly fallback for introspection and tests. Production entrypoints
# use ``application_factory`` so file-based CORS configuration is applied.
app = create_app()
