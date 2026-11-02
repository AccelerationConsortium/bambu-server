"""Dependency-light, swappable advisory empty-bed assessment."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel


class BedAssessment(BaseModel):
    printer_id: str
    snapshot_token: str
    assessed_at: datetime
    state: Literal["clear", "occupied", "unknown"]
    confidence: float
    detections: list[str]
    advisory: Literal[True] = True
    note: str = "Advisory only; a person must still confirm the plate before start_print."


class BedDetector(Protocol):
    def assess(self, *, printer_id: str, snapshot_token: str, image: bytes) -> BedAssessment: ...


class ReferenceDigestDetector:
    """Exact digest reference detector with an intentionally conservative verdict.

    It has no computer-vision dependency and establishes the plugin contract.
    Deployments needing tolerance for lighting/camera changes can replace it
    with a detector implementing :class:`BedDetector`.
    """

    def __init__(self, references: Path | None) -> None:
        self._references = references

    def assess(self, *, printer_id: str, snapshot_token: str, image: bytes) -> BedAssessment:
        reference = self._references / f"{printer_id}.jpg" if self._references else None
        now = datetime.now(UTC)
        if reference is None or not reference.is_file():
            return BedAssessment(
                printer_id=printer_id, snapshot_token=snapshot_token, assessed_at=now,
                state="unknown", confidence=0.0, detections=["no_empty_bed_reference"],
            )
        same = hashlib.sha256(image).digest() == hashlib.sha256(reference.read_bytes()).digest()
        return BedAssessment(
            printer_id=printer_id, snapshot_token=snapshot_token, assessed_at=now,
            state="clear" if same else "occupied",
            confidence=1.0 if same else 0.8,
            detections=[] if same else ["reference_mismatch"],
        )
