"""FastAPI application for the monitoring-only Bambu printer gateway."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
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

BackendFactory = Callable[[PrinterDefinition, PrinterCredentials], PrinterBackend]


def create_app(
    *,
    settings: Settings | None = None,
    backend_factory: BackendFactory = BambuLabsBackend,
) -> FastAPI:
    monitors: dict[str, PrinterMonitor] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_settings = settings or load_settings()
        app.state.settings = active_settings
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
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    def get_monitor(
        printer_id: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]*$")],
    ) -> PrinterMonitor:
        monitor = monitors.get(printer_id)
        if monitor is None:
            raise HTTPException(status_code=404, detail="printer not configured")
        return monitor

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

    return app


def application_factory() -> FastAPI:
    """Load local configuration before constructing middleware and monitors."""

    return create_app(settings=load_settings())


# Import-friendly fallback for introspection and tests. Production entrypoints
# use ``application_factory`` so file-based CORS configuration is applied.
app = create_app()
