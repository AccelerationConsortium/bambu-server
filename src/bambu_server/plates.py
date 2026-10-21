"""Build-plate vocabulary: the slicer's names and the printer's.

Bambu Studio records the plate a job was sliced for as ``curr_bed_type``
("Textured PEI Plate"); the printer's start command wants the same fact as
``bed_type`` ("textured_plate"). The two never meet except here. A plate the
mapping does not know is reported as ``None`` -- never guessed -- and the
dispatch gate refuses an unknown plate rather than defaulting one.
"""

from __future__ import annotations

from .config import BedPlate

#: Slicer ``curr_bed_type`` (lower-cased) to the printer's ``bed_type``.
_SLICER_TO_PRINTER: dict[str, BedPlate] = {
    "cool plate": "cool_plate",
    "cool plate (supertack)": "supertack_plate",
    "supertack plate": "supertack_plate",
    "engineering plate": "eng_plate",
    "high temp plate": "hot_plate",
    "textured pei plate": "textured_plate",
}

#: Printer ``bed_type`` back to the slicer's spelling, for a settings override.
PRINTER_TO_SLICER: dict[BedPlate, str] = {
    "cool_plate": "Cool Plate",
    "supertack_plate": "Supertack Plate",
    "eng_plate": "Engineering Plate",
    "hot_plate": "High Temp Plate",
    "textured_plate": "Textured PEI Plate",
}


def plate_from_slicer(value: str | None) -> BedPlate | None:
    """Translate a ``curr_bed_type`` string, or ``None`` when it is not recognised."""

    if not value:
        return None
    return _SLICER_TO_PRINTER.get(value.strip().lower())


def plate_label(plate: BedPlate | None) -> str:
    return PRINTER_TO_SLICER.get(plate, "undeclared") if plate else "undeclared"
