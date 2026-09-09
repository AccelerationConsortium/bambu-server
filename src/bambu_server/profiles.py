"""Machine profiles — the target a remote submitter validates against.

A profile is the union of two sources with different trust properties:

* the **operator-declared** half (`MachineProfileConfig` in the local YAML):
  bed size, enclosure, safe temperature envelope, forbidden materials. These
  are physical facts about the machine that no telemetry field reports.
* the **observed** half, read from the monitor's cached telemetry: the nozzle
  actually fitted and the AMS trays actually loaded.

Where the two overlap (nozzle type and diameter) the observed value wins when
it exists, because it is the current truth, and the declared value is the
fallback for machines whose live field is blank. Every profile records which
source answered, so a reader can tell a measured fact from a declared one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypeVar

from pydantic import BaseModel, Field

from .config import AmsPolicy, MachineLimits, PrinterDefinition

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .backend import PrinterReading

FieldSource = Literal["observed", "declared", "unknown"]

_T = TypeVar("_T")


class LoadedTray(BaseModel):
    """One loaded AMS filament tray, as the validator needs it.

    A narrower view than the monitor's telemetry record: only the fields a
    compatibility check reads. Tray and tag UUIDs are identifiers, not
    inventory, and are never carried here.
    """

    ams_id: int | None = None
    tray_id: int | None = None
    tray_index: int | None = None
    tray_type: str | None = None
    tray_color: str | None = None
    nozzle_temp_min_c: int | None = None
    nozzle_temp_max_c: int | None = None
    remaining_percent: int | None = None

    @property
    def label(self) -> str:
        location = f"AMS {self.ams_id} tray {self.tray_id}"
        return f"{self.tray_type or 'unknown filament'} ({location})"


class ObservedMachineState(BaseModel):
    """What the monitor's cached telemetry currently says about the machine."""

    telemetry_ok: bool = False
    nozzle_type: str | None = None
    nozzle_diameter_mm: float | None = None
    loaded_trays: list[LoadedTray] = Field(default_factory=list)
    ams_unit_ids: list[int] | None = None

    @classmethod
    def from_reading(cls, reading: PrinterReading | None) -> ObservedMachineState:
        if reading is None or not (reading.connected and reading.data_ready):
            return cls()
        return cls(
            telemetry_ok=True,
            nozzle_type=reading.nozzle_type,
            nozzle_diameter_mm=reading.nozzle_diameter,
            ams_unit_ids=reading.ams_unit_ids,
            loaded_trays=[
                LoadedTray(
                    ams_id=tray.ams_id,
                    tray_id=tray.tray_id,
                    tray_index=tray.tray_index,
                    tray_type=tray.tray_type,
                    tray_color=tray.tray_color,
                    nozzle_temp_min_c=tray.nozzle_temp_min,
                    nozzle_temp_max_c=tray.nozzle_temp_max,
                    remaining_percent=tray.remaining_percent,
                )
                for tray in (reading.ams_trays or [])
            ],
        )


class MachineProfile(BaseModel):
    """The published, submitter-facing description of one machine."""

    id: str
    name: str
    model: str | None = None
    enclosure: Literal["enclosed", "open"] | None = None
    nozzle_type: str | None = None
    nozzle_type_source: FieldSource = "unknown"
    nozzle_diameter_mm: float | None = None
    nozzle_diameter_source: FieldSource = "unknown"
    bed_size_mm: tuple[float, float] | None = None
    chamber_temperature_c: float | None = None
    limits: MachineLimits = Field(default_factory=MachineLimits)
    ams: AmsPolicy = Field(default_factory=AmsPolicy)
    observed: ObservedMachineState = Field(default_factory=ObservedMachineState)
    warnings: list[str] = Field(default_factory=list)

    @property
    def has_chamber_control(self) -> bool:
        """True when the operator declared a chamber temperature for this machine.

        An open-frame P1S declares none; an H2D does. A submitted model that
        asks for a heated chamber cannot run on a machine without one.
        """

        return self.chamber_temperature_c is not None


def _resolve(observed: _T | None, declared: _T | None) -> tuple[_T | None, FieldSource]:
    """Prefer what the printer reports, fall back to what the operator declared."""

    if observed is not None:
        return observed, "observed"
    if declared is not None:
        return declared, "declared"
    return None, "unknown"


def build_profile(
    definition: PrinterDefinition,
    observed: ObservedMachineState,
) -> MachineProfile:
    declared = definition.profile
    nozzle_type, nozzle_type_source = _resolve(observed.nozzle_type, declared.nozzle_type)
    diameter, diameter_source = _resolve(
        observed.nozzle_diameter_mm, declared.nozzle_diameter_mm
    )

    warnings: list[str] = []
    if (
        observed.nozzle_diameter_mm is not None
        and declared.nozzle_diameter_mm is not None
        and abs(observed.nozzle_diameter_mm - declared.nozzle_diameter_mm) > 1e-6
    ):
        # Worth surfacing rather than silently preferring one: it means the
        # declared profile no longer describes the hardware, and every
        # submission validated against it inherits the discrepancy.
        warnings.append(
            "declared nozzle diameter "
            f"{declared.nozzle_diameter_mm} mm does not match the observed "
            f"{observed.nozzle_diameter_mm} mm; the observed value is used"
        )
    if declared.bed_size_mm is None:
        warnings.append("bed_size_mm is not declared; plate fit cannot be checked")
    if not observed.telemetry_ok:
        warnings.append("printer telemetry unavailable; observed fields are omitted")

    return MachineProfile(
        id=definition.id,
        name=definition.name,
        model=definition.model,
        enclosure=declared.enclosure,
        nozzle_type=nozzle_type,
        nozzle_type_source=nozzle_type_source,
        nozzle_diameter_mm=diameter,
        nozzle_diameter_source=diameter_source,
        bed_size_mm=declared.bed_size_mm,
        chamber_temperature_c=declared.chamber_temperature_c,
        limits=declared.limits,
        ams=declared.ams,
        observed=observed,
        warnings=warnings,
    )
