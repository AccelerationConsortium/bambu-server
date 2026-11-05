"""Agent-facing, data-only workflow helpers.

Nothing here reaches a printer or a robot. Object positions are reported in the
printer's bed frame (millimetres, see :class:`~bambu_server.artifacts.ObjectPosition`);
transforming them into an arm's frame is the arm agent's job, not the gateway's.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from .artifacts import ObjectPosition

logger = logging.getLogger(__name__)

#: Version of the event envelope below. Bumped only for incompatible changes;
#: new optional fields do not bump it.
EVENT_SCHEMA_VERSION = 1

EventType = Literal[
    "job.started",
    "job.finished",
    "job.failed",
    "job.rejected",
    "job.cancelled",
    "job.cooled",
    "gateway.restarted",
]

#: Terminal job state to the event announcing it.
TERMINAL_EVENT_TYPES: dict[str, EventType] = {
    "finished": "job.finished",
    "failed": "job.failed",
    "rejected": "job.rejected",
    "cancelled": "job.cancelled",
}


class PickupReadiness(BaseModel):
    """Whether a finished plate has cooled enough to be picked up.

    Read from the monitor's cached telemetry. Advice for the arm agent, never a
    command to anything.
    """

    bed_temperature_c: float | None = None
    safe_bed_temperature_c: float
    cooled: bool
    observed_at: datetime


def pickup_readiness(status: object, safe_bed_c: float) -> PickupReadiness:
    """Readiness from a cached status envelope; ``cooled`` is False when unknown."""

    metric = (getattr(status, "metrics", None) or {}).get("bed_temperature")
    value = getattr(metric, "value", None)
    bed = (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )
    return PickupReadiness(
        bed_temperature_c=bed,
        safe_bed_temperature_c=safe_bed_c,
        cooled=bed is not None and bed <= safe_bed_c,
        observed_at=datetime.now(UTC),
    )


class GatewayEvent(BaseModel):
    """One lifecycle event, as served by ``GET /events`` and POSTed to webhooks.

    ``event_id`` is a zero-padded decimal string that keeps increasing across
    restarts, so ``after=<event_id>`` paging survives one; a
    ``gateway.restarted`` event with a fresh ``boot_id`` marks each restart.
    """

    schema_version: int = EVENT_SCHEMA_VERSION
    event_id: str
    type: EventType
    occurred_at: datetime
    gateway_id: str
    boot_id: str
    submission_id: str | None = None
    printer_id: str | None = None
    state: str | None = None
    updated_at: datetime | None = None
    eln_project: str | None = None
    #: True for a ``job.failed`` the gateway recorded at startup because a
    #: restart interrupted the job's dispatch.
    recovered_after_restart: bool = False
    note: str | None = None
    #: Bed-frame placements from the sliced file (``job.started``, terminal
    #: events and ``job.cooled``).
    object_positions: tuple[ObjectPosition, ...] = ()
    #: Present on ``job.finished`` and ``job.cooled``.
    pickup: PickupReadiness | None = None


class EventPage(BaseModel):
    events: list[GatewayEvent]
    next_after: str | None = None


class EventHub:
    """Durable, bounded event feed with best-effort webhook delivery.

    Events are appended to a JSON-lines log so their ids keep increasing across
    restarts; the newest ``max_events`` are served. A failure to persist or
    deliver an event is logged and never raised to the caller.
    """

    def __init__(
        self,
        *,
        webhook_urls: list[str],
        max_events: int = 1000,
        log_path: Path | None = None,
        gateway_id: str = "bambu_gateway",
    ) -> None:
        self._events: deque[GatewayEvent] = deque(maxlen=max_events)
        self._webhook_urls = webhook_urls
        self._max_events = max_events
        self._log_path = log_path
        self._log_lines = 0
        self._next = 0
        self.gateway_id = gateway_id
        self.boot_id = uuid.uuid4().hex
        self._deliveries: set[asyncio.Task[None]] = set()

    def load(self) -> None:
        """Restore recent events and the id counter from the log."""

        if self._log_path is None or not self._log_path.is_file():
            return
        highest = 0
        lines = 0
        try:
            with self._log_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    lines += 1
                    try:
                        event = GatewayEvent.model_validate_json(line)
                    except ValueError:
                        continue
                    self._events.append(event)
                    if event.event_id.isdigit():
                        highest = max(highest, int(event.event_id))
        except OSError:
            logger.warning("Could not read the event log; event ids restart from it")
        self._next = highest
        self._log_lines = lines

    def page(self, after: str | None, limit: int) -> EventPage:
        events = list(self._events)
        if after:
            events = [event for event in events if event.event_id > after]
        events = events[:limit]
        return EventPage(events=events, next_after=events[-1].event_id if events else after)

    async def publish(self, event_type: EventType, **fields: object) -> GatewayEvent:
        self._next += 1
        event = GatewayEvent(
            event_id=f"{self._next:020d}",
            type=event_type,
            occurred_at=datetime.now(UTC),
            gateway_id=self.gateway_id,
            boot_id=self.boot_id,
            **fields,
        )
        self._events.append(event)
        try:
            await asyncio.to_thread(self._append, event)
        except Exception:
            logger.exception("Could not persist event %s", event.event_id)
        for url in self._webhook_urls:
            task = asyncio.create_task(self._deliver(url, event))
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)
        return event

    def _append(self, event: GatewayEvent) -> None:
        if self._log_path is None:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        self._log_lines += 1
        if self._log_lines > 2 * self._max_events:
            self._compact()

    def _compact(self) -> None:
        assert self._log_path is not None
        temporary = self._log_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for event in self._events:
                handle.write(event.model_dump_json() + "\n")
        temporary.replace(self._log_path)
        self._log_lines = len(self._events)

    async def _deliver(self, url: str, event: GatewayEvent) -> None:
        def send() -> None:
            request = Request(
                url, data=event.model_dump_json().encode(), method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=5):  # noqa: S310 - configured operator endpoint
                pass
        try:
            await asyncio.to_thread(send)
        except Exception:
            logger.exception("Could not deliver gateway event to configured webhook")


class PickupInfo(BaseModel):
    """A finished job's objects in bed coordinates, and whether the bed is cool.

    No robot frame: the arm agent applies its own calibration.
    """

    submission_id: str
    printer_id: str
    state: str
    ready: bool
    reason: str
    bed_size_mm: tuple[float, float] | None = None
    #: The Y axis position the job's end gcode parked the bed at, when the
    #: gateway set it while slicing. Object positions are in the bed's own
    #: frame and move with it, so the arm needs this to find them; None means
    #: unknown (the stock end move, or a file sliced elsewhere).
    bed_park_y_mm: float | None = None
    pickup: PickupReadiness
    objects: list[ObjectPosition] = Field(default_factory=list)
