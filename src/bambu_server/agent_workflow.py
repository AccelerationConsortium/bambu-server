"""Agent-facing, data-only workflow helpers.

No type in this module reaches a printer or a robot.  It persists calibration
and event records locally; callers decide whether to forward events elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field, model_validator

from .artifacts import ObjectPosition

logger = logging.getLogger(__name__)


class GatewayEvent(BaseModel):
    event_id: str
    type: Literal["job.started", "job.finished", "job.failed"]
    submission_id: str
    printer_id: str
    occurred_at: datetime
    object_positions: tuple[ObjectPosition, ...] = ()


class EventPage(BaseModel):
    events: list[GatewayEvent]
    next_after: str | None = None


class BedToRobotCalibration(BaseModel):
    """Rigid 2D placement transform from bed millimetres to arm coordinates."""

    printer_id: str
    arm_frame: str = Field(min_length=1, max_length=120)
    # Homogeneous row-major 4x4 matrix.  Coordinates are (x, y, z, 1).
    matrix: tuple[float, ...] = Field(min_length=16, max_length=16)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def affine_last_row(self) -> BedToRobotCalibration:
        if tuple(self.matrix[12:]) != (0.0, 0.0, 0.0, 1.0):
            raise ValueError("matrix must be an affine 4x4 transform with last row [0,0,0,1]")
        return self


class PickupTarget(BaseModel):
    object_id: str
    name: str | None
    arm_frame: str
    x: float
    y: float
    z: float
    rotation_deg: float | None = None


class PickupPlan(BaseModel):
    submission_id: str
    printer_id: str
    ready: bool
    reason: str
    bed_temperature_c: float | None = None
    safe_bed_temperature_c: float
    targets: list[PickupTarget] = Field(default_factory=list)


class CalibrationStore:
    def __init__(self, root: Path) -> None:
        self._path = root / "bed_to_robot_calibrations.json"
        self._values: dict[str, BedToRobotCalibration] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        if not self._path.is_file():
            return
        try:
            values = json.loads(self._path.read_text(encoding="utf-8"))
            self._values = {
                value["printer_id"]: BedToRobotCalibration.model_validate(value)
                for value in values if isinstance(value, dict)
            }
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable robot calibration store")

    def get(self, printer_id: str) -> BedToRobotCalibration | None:
        return self._values.get(printer_id)

    async def set(self, calibration: BedToRobotCalibration) -> BedToRobotCalibration:
        async with self._lock:
            self._values[calibration.printer_id] = calibration
            payload = [value.model_dump(mode="json") for value in self._values.values()]
            await asyncio.to_thread(self._write, payload)
            return calibration

    def _write(self, payload: list[dict[str, object]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self._path)


class EventHub:
    """Bounded event feed with best-effort asynchronous JSON webhook delivery."""

    def __init__(self, *, webhook_urls: list[str], max_events: int = 1000) -> None:
        self._events: deque[GatewayEvent] = deque(maxlen=max_events)
        self._webhook_urls = webhook_urls
        self._next = 0
        self._deliveries: set[asyncio.Task[None]] = set()

    def page(self, after: str | None, limit: int) -> EventPage:
        events = list(self._events)
        if after:
            events = [event for event in events if event.event_id > after]
        events = events[:limit]
        return EventPage(events=events, next_after=events[-1].event_id if events else after)

    async def publish(
        self, event_type: Literal["job.started", "job.finished", "job.failed"], *,
        submission_id: str, printer_id: str, object_positions: tuple[ObjectPosition, ...] = (),
    ) -> GatewayEvent:
        self._next += 1
        event = GatewayEvent(
            event_id=f"{self._next:020d}",
            type=event_type,
            submission_id=submission_id,
            printer_id=printer_id,
            occurred_at=datetime.now(UTC),
            object_positions=object_positions,
        )
        self._events.append(event)
        for url in self._webhook_urls:
            task = asyncio.create_task(self._deliver(url, event))
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)
        return event

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


def transform_target(position: ObjectPosition, calibration: BedToRobotCalibration) -> PickupTarget:
    m = calibration.matrix
    x, y, z = position.center_x_mm, position.center_y_mm, 0.0
    return PickupTarget(
        object_id=position.object_id, name=position.name, arm_frame=calibration.arm_frame,
        x=round(m[0] * x + m[1] * y + m[2] * z + m[3], 3),
        y=round(m[4] * x + m[5] * y + m[6] * z + m[7], 3),
        z=round(m[8] * x + m[9] * y + m[10] * z + m[11], 3),
        rotation_deg=position.rotation_deg,
    )
