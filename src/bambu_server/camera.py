"""View-only camera frames, shared and rate-limited (approved 2026-10-21).

A person choosing a printer, or watching its job, can see the chamber camera
without holding a claim. That is printer I/O outside ``/control/*``, so it is
held to three limits:

* **Shared.** At most one camera read per printer per ``min_interval_s``,
  however many pages are open; every viewer gets the same cached frame.
* **No commands.** A frame read opens the camera port only; nothing is
  published to the printer.
* **Not evidence.** A viewer frame carries no snapshot token, so it can never
  stand in for the plate check: ``start_print`` still needs a snapshot taken
  under the claim.

A failed read is cached for the same interval, so an unreachable camera is not
retried by every poll.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from .backend import PrinterBackend, PrinterCommandError

#: The approved refresh: one read per printer every 2 s at most.
DEFAULT_MIN_INTERVAL_S = 2.0


class CameraUnavailable(RuntimeError):
    """The printer's camera did not deliver a frame. The message is safe to show."""


@dataclass(frozen=True)
class CameraFrame:
    image: bytes
    captured_at: datetime


class CameraViewer:
    def __init__(
        self,
        backends: dict[str, PrinterBackend],
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        timeout_s: float = 5.0,
    ) -> None:
        self._backends = backends
        self._min_interval_s = min_interval_s
        self._timeout_s = timeout_s
        self._locks: dict[str, asyncio.Lock] = {}
        # printer id -> (monotonic read time, frame or failure message)
        self._latest: dict[str, tuple[float, CameraFrame | str]] = {}

    async def frame(self, printer_id: str) -> CameraFrame:
        lock = self._locks.setdefault(printer_id, asyncio.Lock())
        async with lock:
            cached = self._latest.get(printer_id)
            if cached is None or monotonic() - cached[0] >= self._min_interval_s:
                cached = (monotonic(), await self._read(printer_id))
                self._latest[printer_id] = cached
        result = cached[1]
        if isinstance(result, str):
            raise CameraUnavailable(result)
        return result

    async def _read(self, printer_id: str) -> CameraFrame | str:
        try:
            image = await asyncio.to_thread(
                self._backends[printer_id].snapshot, self._timeout_s
            )
        except PrinterCommandError as exc:
            return f"camera unavailable: {exc}"
        return CameraFrame(image=image, captured_at=datetime.now(UTC))
