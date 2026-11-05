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
import contextlib
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path as PathLib
from pathlib import PurePosixPath
from typing import Annotated, Literal

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
from .agent_mode import (
    AgentCaller,
    AgentModeGate,
    AgentModeRefusal,
    AgentModeStore,
    AgentModeView,
    agent_caller,
)
from .agent_workflow import EventHub, EventPage, PickupInfo
from .artifacts import ARTIFACT_EXTENSIONS, ObjectPosition
from .backend import BambuLabsBackend, PrinterBackend, camera_snapshot_supported
from .camera import CameraUnavailable, CameraViewer
from .claims import ClaimConflict, ClaimLost
from .config import (
    PrinterCredentials,
    PrinterDefinition,
    Settings,
    load_settings,
    resolve_credentials,
    resolve_edge_secret,
    resolve_results_ingest,
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
from .documentation import RefusalBody, register_documentation
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
from .records import (
    OUTBOX_STATES,
    OutboxConflict,
    OutboxEntry,
    OutboxStore,
    OutboxWorker,
    RecordKeeper,
    ResultBundle,
    ResultStore,
    Transport,
    urllib_transport,
)
from .slicer import (
    BambuStudioSlicer,
    Placement,
    SliceRequest,
    SlicerError,
    SlicerUnavailable,
)
from .submissions import (
    TERMINAL_STATES as TERMINAL_JOB_STATES,
)
from .submissions import (
    ArtifactTooLarge,
    InvalidTransition,
    JobState,
    SliceProvenance,
    SubmissionError,
    SubmissionJob,
    SubmissionStore,
    clean_text,
    run_validation,
    safe_display_name,
)
from .vision import BedAssessment, ReferenceDigestDetector

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
    #: Optional ELN project for the finished record; replaces one chosen at
    #: submission. Filing happens only through the central server's ingest.
    eln_project: str | None = Field(default=None, min_length=1, max_length=120)


class IngestRequest(BaseModel):
    """Queue a terminal job's record for filing through the central server."""

    eln_project: str = Field(min_length=1, max_length=120)
    #: Ignored when the edge supplies a verified identity.
    requested_by: str | None = Field(default=None, min_length=1, max_length=120)


class AgentModeRequest(BaseModel):
    """An admin's decision to switch agent mode, with the reason for the audit log."""

    enabled: bool
    reason: str = Field(min_length=3, max_length=500)


class OutboxView(BaseModel):
    ingest_configured: bool
    counts: dict[str, int]
    entries: list[OutboxEntry]


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
    results_transport: Transport = urllib_transport,
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
    cameras: dict[str, CameraViewer] = {}
    workflows: dict[str, object] = {}
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
        results_settings = active_settings.results
        hub = EventHub(
            webhook_urls=active_settings.agent_workflow.event_webhook_urls,
            max_events=active_settings.agent_workflow.event_buffer_size,
            log_path=store.root / "events.jsonl",
            gateway_id=results_settings.gateway_id,
        )
        await asyncio.to_thread(hub.load)
        results = ResultStore(store.root)
        outbox = OutboxStore(store.root)
        await asyncio.to_thread(outbox.load)
        ingest_url, ingest_token = resolve_results_ingest(results_settings)
        if ingest_url and not ingest_token:
            logger.warning(
                "A results ingest URL is configured without BAMBU_RESULTS_TOKEN; "
                "records stay local_only until the token is set"
            )
        workflows.update(
            events=hub,
            results=results,
            outbox=outbox,
            detector=ReferenceDigestDetector(
                active_settings.agent_workflow.empty_bed_reference_directory
            ),
        )
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
        agent_store = AgentModeStore(store.root)
        await asyncio.to_thread(agent_store.load)
        agent_gate = AgentModeGate(
            agent_store,
            {p.id for p in active_settings.printers if p.agent_mode_eligible},
        )
        workflows["agent_mode"] = agent_gate
        dispatcher = Dispatcher(
            settings=active_settings.dispatch,
            store=store,
            monitors=monitors,
            backends=backends,
            poll_interval_s=active_settings.poll_interval_seconds,
            agent_mode=agent_gate,
        )
        dispatchers["default"] = dispatcher
        cameras["default"] = CameraViewer(
            backends,
            unsupported=frozenset(
                p.id for p in active_settings.printers if not camera_snapshot_supported(p.model)
            ),
        )
        slicers["default"] = BambuStudioSlicer(active_settings.slicer)
        keeper = RecordKeeper(
            store=store,
            results=results,
            outbox=outbox,
            events=hub,
            monitors=monitors,
            gateway_id=results_settings.gateway_id,
            gateway_version=__version__,
            pickup_safe_bed_c=active_settings.agent_workflow.pickup_safe_bed_c,
            ingest_configured=bool(ingest_url and ingest_token),
            cooldown_poll_s=active_settings.poll_interval_seconds,
            cooldown_timeout_s=active_settings.agent_workflow.cooldown_watch_timeout_s,
        )
        workflows["keeper"] = keeper
        store.add_listener(keeper.notify)
        worker_task: asyncio.Task[None] | None = None
        try:
            await keeper.start()
            if ingest_url and ingest_token:
                worker = OutboxWorker(
                    outbox=outbox,
                    results=results,
                    url=ingest_url,
                    token=ingest_token,
                    gateway_id=results_settings.gateway_id,
                    poll_interval_s=results_settings.poll_interval_s,
                    retry_initial_s=results_settings.retry_initial_s,
                    retry_max_s=results_settings.retry_max_s,
                    request_timeout_s=results_settings.request_timeout_s,
                    transport=results_transport,
                )
                workflows["worker"] = worker
                worker_task = asyncio.create_task(worker.run(results_settings.worker_tick_s))
            for monitor in monitors.values():
                await monitor.start()
            if active_settings.dispatch.enabled:
                await dispatcher.start()
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
            await keeper.stop()
            await dispatcher.stop()
            for monitor in reversed(list(monitors.values())):
                await monitor.stop()
            monitors.clear()
            stores.clear()
            dispatchers.clear()
            slicers.clear()
            workflows.clear()

    app = FastAPI(
        title="AC Bambu Printer Gateway",
        version=__version__,
        description=(
            "MQTT monitoring gateway with optional claimed control for Bambu Lab printers. Each configured "
            "printer is exposed through the AC lab equipment status spec v1.2. "
            "Profiles expose typed AMS inventory, color-label provenance, and remaining "
            "filament estimates. Dispatch can be enabled locally for claimed starts after a "
            "plate-clear check (a person, or an agent where a printer opts in). Every job "
            "keeps a durable, schema-versioned result bundle and its exact artifact; "
            "filing to the ELN happens only through the central server's ingest. Agent "
            "workflow routes expose bed-frame object positions, lifecycle events and "
            "cool-down readiness; they never move a robot. Use the authenticated /bambu/ "
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
            swagger_ui_parameters={"supportedSubmitMethods": []},
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

    def get_agent_gate() -> AgentModeGate:
        value = workflows.get("agent_mode")
        if not isinstance(value, AgentModeGate):  # pragma: no cover - outside lifespan
            raise HTTPException(status_code=503, detail="agent mode unavailable")
        return value

    def get_agent(
        request: Request, actor: Annotated[Actor, Depends(get_actor)]
    ) -> AgentCaller:
        roles = app.state.settings.dispatch.agent_roles
        return agent_caller(actor, request.headers, agent_roles=roles)

    def require_agent_mode(printer_id: str, agent: AgentCaller, action: str) -> None:
        if not agent.is_agent:
            return
        try:
            get_agent_gate().require(printer_id, action)
        except AgentModeRefusal as exc:
            raise HTTPException(status_code=exc.status, detail=exc.body) from exc

    def get_keeper() -> RecordKeeper:
        value = workflows.get("keeper")
        if not isinstance(value, RecordKeeper):  # pragma: no cover - outside lifespan
            raise HTTPException(status_code=503, detail="record keeper unavailable")
        return value

    def get_results() -> ResultStore:
        value = workflows.get("results")
        if not isinstance(value, ResultStore):  # pragma: no cover - outside lifespan
            raise HTTPException(status_code=503, detail="result store unavailable")
        return value

    def get_outbox() -> OutboxStore:
        value = workflows.get("outbox")
        if not isinstance(value, OutboxStore):  # pragma: no cover - outside lifespan
            raise HTTPException(status_code=503, detail="outbox unavailable")
        return value

    def get_events() -> EventHub:
        value = workflows.get("events")
        if not isinstance(value, EventHub):
            raise HTTPException(status_code=503, detail="event feed unavailable")
        return value

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
            details={
                "monitoring_only": not control_enabled,
                "printer_count": len(monitors),
                "agent_mode": _agent_mode_summaries(),
                **_outbox_details(),
            },
        )

    def _agent_mode_summaries() -> dict[str, object]:
        gate = workflows.get("agent_mode")
        if not isinstance(gate, AgentModeGate):
            return {}
        return {printer_id: gate.summary(printer_id) for printer_id in monitors}

    def _outbox_details() -> dict[str, object]:
        outbox = workflows.get("outbox")
        keeper = workflows.get("keeper")
        counts = (
            outbox.counts() if isinstance(outbox, OutboxStore)
            else dict.fromkeys(OUTBOX_STATES, 0)
        )
        return {
            "results_ingest_configured": bool(
                isinstance(keeper, RecordKeeper) and keeper.ingest_configured
            ),
            "results_outbox": counts,
            "results_pending": counts["pending"] + counts["delivered"],
            "results_filing": counts["filing"],
            "results_held": counts["held"],
        }

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
        ``details.agent_mode`` reports whether agent mode is available for this
        printer and switched on, and who last flipped it, when and why.
        """
        status = monitor.status()
        printer_id = monitor.definition.id
        gate = workflows.get("agent_mode")
        agent_details = (
            {"agent_mode": gate.summary(printer_id)} if isinstance(gate, AgentModeGate) else {}
        )
        if not control_enabled:
            return status.model_copy(update={"details": {**status.details, **agent_details}})
        dispatcher = get_dispatcher()
        holder = dispatcher.claims(printer_id).holder()
        details = {
            **status.details,
            **agent_details,
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
        agent: Annotated[AgentCaller, Depends(get_agent)],
        target_machine: Annotated[str, Form(max_length=120)],
        requested_by: Annotated[str | None, Form(max_length=120)] = None,
        material: Annotated[str | None, Form(max_length=60)] = None,
        eln_project: Annotated[str | None, Form(max_length=120)] = None,
        placement: Annotated[
            Placement | None,
            Form(
                description="Where the gateway puts an uploaded .stl on the plate: "
                "`auto` (default; the slicer centres it) or `front_left` (a margin in "
                "from the front-left corner of the bed). Only for an .stl."
            ),
        ] = None,
    ) -> SubmissionJob:
        """Accept a print artifact, validate it, and queue it if it passes.

        Performs no printer I/O. Validation runs inline so the caller gets the
        per-check verdict in the response; the file read happens on a worker
        thread so a large artifact does not stall the status poll loop.
        ``eln_project`` optionally names the ELN project the finished record
        is filed under, through the central server. An agent (``X-Agent-Id`` or
        an agent edge role) may submit only to a printer in agent mode.
        ``placement`` positions an ``.stl`` the gateway slices; a sliced file
        is printed where its slicer put it, so it is refused there.
        """

        # A verified identity wins over the form field: a signed-in person must
        # not be able to submit under someone else's name.
        try:
            owner, owner_verified = actor_for(actor, requested_by or agent.agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="requested_by is required") from exc

        monitor = monitors.get(target_machine)
        if monitor is None:
            raise HTTPException(status_code=404, detail="unknown target machine")
        require_agent_mode(monitor.definition.id, agent, "submission")
        agent_id = agent.agent_id if agent.is_agent else None

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
                eln_project=eln_project,
                agent_id=agent_id,
                placement=placement or "auto",
            )
        if placement is not None:
            raise HTTPException(
                status_code=422,
                detail="placement applies only to an .stl the gateway slices; "
                "a sliced file prints where its slicer placed it",
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
                eln_project=eln_project,
                agent_id=agent_id,
            )
        except ArtifactTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except SubmissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
        eln_project: str | None = None,
        agent_id: str | None = None,
        placement: Placement = "auto",
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
                + ", ".join(slicer.materials(printer_id)) + ")",
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
                placement=placement,
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
                placement=placement,
                end_bed_y_mm=result.end_bed_y_mm,
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
                    eln_project=eln_project,
                    agent_id=agent_id,
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
        updated_after: Annotated[datetime | None, Query()] = None,
        order: Annotated[Literal["submitted", "newest"], Query()] = "submitted",
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[SubmissionJob]:
        """List jobs.

        Default order is oldest submission first. ``order=newest`` sorts by most
        recent update; with ``updated_after`` and ``offset`` that pages through
        changes since a client's last sync.
        """

        jobs = store.list(
            machine=machine,
            state=state,
            updated_after=updated_after,
            newest_first=order == "newest",
        )
        return jobs[offset:offset + limit]

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

    @app.get(
        "/submissions/{submission_id}/artifact",
        response_class=FileResponse,
        responses={
            200: {"description": "The exact stored artifact; ETag is its quoted SHA-256."},
            304: {"description": "If-None-Match matched the artifact's SHA-256."},
            410: {"description": "The artifact was deleted (cancel opt-in or retention)."},
        },
        tags=["submissions"],
    )
    async def submission_artifact(
        request: Request,
        store: Annotated[SubmissionStore, Depends(get_store)],
        actor: Annotated[Actor, Depends(get_actor)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> Response:
        """Download the exact sliced file that was submitted (and printed).

        A read, like every other GET here: access is gated by the lab edge, and
        the download is logged with the caller's identity when the edge
        verified one. Reads the gateway's own disk only.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        path = store.artifact_path(job)
        if job.artifact_removed or not path.is_file():
            raise HTTPException(status_code=410, detail="the artifact is no longer stored")
        etag = f'"{job.sha256}"'
        if etag in [tag.strip() for tag in request.headers.get("if-none-match", "").split(",")]:
            return Response(status_code=304, headers={"ETag": etag})
        logger.info(
            "Artifact of %s downloaded by %s%s",
            submission_id,
            actor.user or "an unidentified caller",
            " (verified)" if actor.verified else "",
        )
        return FileResponse(
            path,
            media_type="model/3mf" if job.artifact_kind == "3mf" else "text/x.gcode",
            filename=job.original_filename,
            headers={"ETag": etag, "X-Content-SHA256": job.sha256},
        )

    @app.get(
        "/submissions/{submission_id}/result",
        response_model=ResultBundle,
        tags=["submissions"],
    )
    async def submission_result(
        store: Annotated[SubmissionStore, Depends(get_store)],
        results: Annotated[ResultStore, Depends(get_results)],
        keeper: Annotated[RecordKeeper, Depends(get_keeper)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> Response:
        """The job's schema-versioned result bundle.

        For a terminal job, the bundle written when it reached that state
        (``final: true``); it survives even ``DELETE`` of the job record. For a
        job still in play, one built now (``final: false``).
        """

        stored = results.read(submission_id)
        if stored is not None:
            return Response(content=stored, media_type="application/json")
        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        bundle = keeper.bundle(
            job, objects=await keeper.positions(job), pickup=None, final=False
        )
        return Response(content=bundle.model_dump_json(), media_type="application/json")

    @app.get(
        "/submissions/{submission_id}/objects",
        response_model=list[ObjectPosition],
        tags=["agent-workflow"],
    )
    async def submission_objects(
        store: Annotated[SubmissionStore, Depends(get_store)],
        keeper: Annotated[RecordKeeper, Depends(get_keeper)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> list[ObjectPosition]:
        """Where each sliced object sits on the plate, in bed coordinates (mm).

        Origin at the front-left corner of the printable area, +X right, +Y
        toward the back seen from the front, as the printer's own toolpath
        addresses it; compare with ``bed_size_mm`` from the printer's profile.
        No robot frame: the arm agent applies its own calibration. Empty when
        the file does not say where its objects are.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        if not keeper.artifact_stored(job):
            raise HTTPException(status_code=410, detail="the artifact is no longer stored")
        return list(await keeper.positions(job))

    @app.get("/events", response_model=EventPage, tags=["agent-workflow"])
    async def workflow_events(
        events: Annotated[EventHub, Depends(get_events)],
        after: Annotated[str | None, Query(max_length=40)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventPage:
        """Poll lifecycle events. Ids keep increasing across restarts; a
        ``gateway.restarted`` event marks each one. Webhooks get the same events."""

        return events.page(after, limit)

    @app.get(
        "/submissions/{submission_id}/pickup-targets",
        response_model=PickupInfo,
        tags=["agent-workflow"],
    )
    async def pickup_targets(
        store: Annotated[SubmissionStore, Depends(get_store)],
        keeper: Annotated[RecordKeeper, Depends(get_keeper)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> PickupInfo:
        """A finished job's objects in bed coordinates, with cool-down readiness.

        Reads the stored artifact and the monitor's cached bed temperature only;
        never contacts a printer or a robot. ``ready`` needs a finished job, a
        cooled bed and at least one known object position.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        pickup = keeper.readiness(job.target_machine)
        if pickup is None:  # pragma: no cover - a job's machine is always configured
            raise HTTPException(status_code=404, detail="printer not configured")
        objects = list(await keeper.positions(job))
        if job.state != "finished":
            ready, reason = False, f"the job is {job.state}, not finished"
        elif not pickup.cooled:
            ready, reason = False, "the bed is not cool enough, or its temperature is unknown"
        elif not objects:
            ready, reason = False, "the sliced file does not say where its objects are"
        else:
            ready, reason = True, "finished and cooled"
        return PickupInfo(
            submission_id=job.submission_id,
            printer_id=job.target_machine,
            state=job.state,
            ready=ready,
            reason=reason,
            bed_size_mm=keeper.bed_size(job.target_machine),
            bed_park_y_mm=job.provenance.end_bed_y_mm if job.provenance else None,
            pickup=pickup,
            objects=objects,
        )

    @app.post(
        "/submissions/{submission_id}/eln-ingest",
        response_model=OutboxEntry,
        responses={409: {"description": "Not terminal, already queued, or no ingest configured."}},
        tags=["results"],
    )
    async def queue_eln_ingest(
        store: Annotated[SubmissionStore, Depends(get_store)],
        outbox: Annotated[OutboxStore, Depends(get_outbox)],
        keeper: Annotated[RecordKeeper, Depends(get_keeper)],
        actor: Annotated[Actor, Depends(get_actor)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
        request: IngestRequest,
    ) -> OutboxEntry:
        """File a finished job's record later: pick an ELN project and queue it.

        Queues the result bundle in the on-disk outbox for the central server's
        ingest, which alone writes to the ELN. A verified identity is recorded
        as who queued it. Refused while central ingest is not configured.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        try:
            who, verified = actor_for(actor, request.requested_by)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="requested_by is required") from exc
        project = clean_text(request.eln_project, field="eln_project", required=True)
        if not keeper.results_exists(job.submission_id) and job.state in TERMINAL_JOB_STATES:
            await keeper.write_final_bundle(job)
        try:
            return await outbox.queue(
                job,
                eln_project=project or "",
                queued_by=who,
                verified=verified,
                ingest_configured=keeper.ingest_configured,
            )
        except OutboxConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/submissions/{submission_id}/eln-ingest",
        response_model=OutboxEntry,
        tags=["results"],
    )
    async def read_eln_ingest(
        outbox: Annotated[OutboxStore, Depends(get_outbox)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    ) -> OutboxEntry:
        entry = outbox.get(submission_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="no outbox entry for this submission")
        return entry

    @app.get("/results/outbox", response_model=OutboxView, tags=["results"])
    async def results_outbox(
        outbox: Annotated[OutboxStore, Depends(get_outbox)],
        keeper: Annotated[RecordKeeper, Depends(get_keeper)],
        state: Annotated[str | None, Query(max_length=20)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> OutboxView:
        return OutboxView(
            ingest_configured=keeper.ingest_configured,
            counts=outbox.counts(),
            entries=outbox.entries(state)[:limit],
        )

    @app.get(
        "/printers/{printer_id}/agent-mode",
        response_model=AgentModeView,
        tags=["agent-mode"],
    )
    async def read_agent_mode(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        gate: Annotated[AgentModeGate, Depends(get_agent_gate)],
    ) -> AgentModeView:
        """Whether agents may run this printer end to end, with the audit trail."""

        return gate.view(monitor.definition.id)

    @app.post(
        "/printers/{printer_id}/agent-mode",
        response_model=AgentModeView,
        responses={
            403: {"description": "Not an edge-verified admin, or the caller is an agent."},
            409: {"description": "Agent mode is not available for this printer."},
        },
        tags=["agent-mode"],
    )
    async def set_agent_mode(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        gate: Annotated[AgentModeGate, Depends(get_agent_gate)],
        actor: Annotated[Actor, Depends(get_actor)],
        agent: Annotated[AgentCaller, Depends(get_agent)],
        request: AgentModeRequest,
    ) -> AgentModeView:
        """Switch agent mode on or off. Edge-verified admins only; always audited.

        Turning it off stops new agent submissions, approvals and starts at
        once; a job already printing is not affected.
        """

        admin_roles = app.state.settings.dispatch.agent_mode_admin_roles
        if not (actor.verified and actor.user and actor.role in admin_roles):
            raise HTTPException(
                status_code=403,
                detail="only an edge-verified admin can switch agent mode",
            )
        if agent.is_agent:
            raise HTTPException(
                status_code=403, detail="an agent cannot switch agent mode, even as an admin"
            )
        printer_id = monitor.definition.id
        if printer_id not in gate.eligible:
            raise HTTPException(
                status_code=409,
                detail=f"agent mode is not available for {printer_id}; "
                "it is approved for the Bambu A1 only",
            )
        reason = clean_text(request.reason, field="reason", required=True) or ""
        await gate.store.set(
            printer_id,
            enabled=request.enabled,
            changed_by=actor.user,
            role=actor.role,
            reason=reason,
        )
        return gate.view(printer_id)

    @app.post(
        "/submissions/{submission_id}/approve",
        response_model=SubmissionJob,
        tags=["submissions"],
    )
    async def approve_submission(
        store: Annotated[SubmissionStore, Depends(get_store)],
        actor: Annotated[Actor, Depends(get_actor)],
        agent: Annotated[AgentCaller, Depends(get_agent)],
        submission_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
        approval: ApprovalRequest,
    ) -> SubmissionJob:
        """Record sign-off on a queued submission.

        This is the approval gate and nothing more: it moves no hardware, starts
        no print, and reaches no printer. It marks the job ``approved`` and sets
        ``verdict.dispatch_ready``, which dispatch requires. An agent may approve
        only on a printer in agent mode. ``eln_project`` optionally sets where
        the finished record is filed.
        """

        job = store.get(submission_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown submission")
        require_agent_mode(job.target_machine, agent, "approval")
        try:
            who, verified = actor_for(actor, approval.approved_by or agent.agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="approved_by is required") from exc
        try:
            return await store.approve(
                job,
                approved_by=who,
                verified=verified,
                eln_project=approval.eln_project,
                agent_id=agent.agent_id if agent.is_agent else None,
            )
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SubmissionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    def get_camera() -> CameraViewer:
        camera = cameras.get("default")
        if camera is None:  # pragma: no cover - only outside the app lifespan
            raise HTTPException(status_code=503, detail="camera unavailable")
        return camera

    if control_enabled:
        _register_control_routes(app, get_monitor, get_actor, get_dispatcher, get_agent)
        _register_camera_route(app, get_monitor, get_camera)
        if settings and settings.agent_workflow.vision_enabled:
            _register_advisory_bed_check(app, get_monitor, get_dispatcher, workflows)

    register_documentation(app)
    return app


def _register_advisory_bed_check(
    app: FastAPI,
    get_monitor: Callable[..., PrinterMonitor],
    get_dispatcher: Callable[[], Dispatcher],
    workflows: dict[str, object],
) -> None:
    """Assess a previously captured control snapshot without printer I/O.

    Advisory input only: the result is not itself a plate check. A person
    still confirms the plate, or, on a printer in agent mode, the agent makes
    its own ``agent_camera`` decision and may use this as one input.
    """

    class BedCheckRequest(BaseModel):
        snapshot_token: str = Field(min_length=1, max_length=64)

    @app.post(
        "/printers/{printer_id}/bed-check",
        response_model=BedAssessment,
        tags=["agent-workflow"],
    )
    async def advisory_bed_check(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        request: BedCheckRequest,
    ) -> BedAssessment:
        detector = workflows.get("detector")
        if not isinstance(detector, ReferenceDigestDetector):
            raise HTTPException(status_code=503, detail="bed detector unavailable")
        image = dispatcher.snapshot_image(request.snapshot_token, printer_id=monitor.definition.id)
        if image is None:
            raise HTTPException(status_code=404, detail="snapshot token is unknown or expired")
        return await asyncio.to_thread(
            detector.assess,
            printer_id=monitor.definition.id,
            snapshot_token=request.snapshot_token,
            image=image,
        )


def _register_camera_route(
    app: FastAPI,
    get_monitor: Callable[..., PrinterMonitor],
    get_camera: Callable[[], CameraViewer],
) -> None:
    """The view-only camera (approved 2026-10-21). Control deployments only."""

    @app.get(
        "/printers/{printer_id}/camera.jpg",
        response_class=Response,
        responses={
            200: {"content": {"image/jpeg": {}}, "description": "The latest shared frame."},
            503: {"description": "The camera did not deliver a frame."},
        },
        tags=["camera"],
    )
    async def camera_frame(
        monitor: Annotated[PrinterMonitor, Depends(get_monitor)],
        camera: Annotated[CameraViewer, Depends(get_camera)],
    ) -> Response:
        """The printer's chamber camera, for watching -- not for the plate check.

        No claim, no command: one camera read per printer every 2 s at most,
        shared by every viewer. A frame from here is never evidence that the
        plate is empty; a camera ``start_print`` captures its own under the claim.
        """

        try:
            frame = await camera.frame(monitor.definition.id)
        except CameraUnavailable as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})
        return Response(
            content=frame.image,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Captured-At": frame.captured_at.isoformat(),
            },
        )


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
    get_agent: Callable[..., AgentCaller],
) -> None:
    """STATUS_SPEC §5 claims plus the four control verbs, per printer.

    Only called when the local config enables dispatch, so a deployment that
    has not opted in exposes no ``/control/`` path at all.
    """

    control_responses = {
        403: {"model": RefusalBody, "description": "Verified identity or agent mode required."},
        412: {"model": RefusalBody, "description": "A precondition is not met; the body names which."},
        423: {"model": RefusalBody, "description": "No live claim token was presented."},
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
        responses={
            423: control_responses[423],
            501: {"model": RefusalBody, "description": "Snapshot unsupported for this printer model."},
            502: {"model": RefusalBody, "description": "Camera unreachable."},
        },
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
        agent: Annotated[AgentCaller, Depends(get_agent)],
        dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
        request: StartPrintRequest,
        claim_token: Annotated[str | None, Header(alias=CLAIM_HEADER)] = None,
    ) -> SubmissionJob | JSONResponse:
        """Upload an approved job to the printer and start it.

        Requires the claim, a statement that the build plate is empty, an
        explicit tray for every filament, and every gate in
        :mod:`bambu_server.dispatch`. A person states the plate is empty
        (``in_person`` or ``printer_camera``). An agent may start only on a
        printer whose agent mode an admin switched on, and must decide itself
        from a fresh claimed snapshot: ``plate_check_method: agent_camera`` with
        that ``snapshot_token`` and an optional ``plate_check_rationale``.
        Returns once the printer reports the job running, or with the job
        ``failed`` and the uncertainty recorded.
        """

        try:
            return await dispatcher.start_print(
                monitor.definition.id, request, actor=actor, claim_token=claim_token,
                agent=agent,
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
