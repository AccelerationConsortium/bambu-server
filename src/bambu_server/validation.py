"""Model validation — does this artifact belong on *this* machine?

Pure analysis. Given the observations in :class:`~bambu_server.artifacts.ArtifactFacts`
and the target's :class:`~bambu_server.profiles.MachineProfile`, produce a
per-check verdict. Nothing here reads a file, opens a socket, or touches a
printer, which is what makes it safe to run on submission and re-run later.

Three outcomes exist per check, and the distinction is the whole point:

``pass``
    The check ran and the model satisfied it.
``fail``
    The check ran and the model did not. One failing check rejects the
    submission.
``not_applicable``
    The check could not run because an input does not exist -- no AMS tray
    inventory, no declared bed size, no temperature limits. It is recorded with
    the reason and does **not** count as a pass. A silent pass on missing data
    is the failure mode this shape exists to prevent.

This module is the natural body of a ``lab_skills`` skill (``bambu.validate_model``):
it takes an artifact and a machine profile and returns a verdict, so wrapping it
as a skill later needs no change to the checks themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .artifacts import ArtifactFacts
from .config import TemperatureBand
from .profiles import LoadedTray, MachineProfile

CheckStatus = Literal["pass", "fail", "not_applicable"]

CHECK_ORDER = (
    "machine_compatible",
    "material_allowed",
    "material_filament_match",
    "nozzle_temp_in_band",
    "bed_chamber_temp_in_band",
    "build_fits_plate",
    "gcode_sanity",
    "params_present",
)


class CheckResult(BaseModel):
    check: str
    status: CheckStatus
    #: ``False`` only when this check blocks dispatch. A ``not_applicable``
    #: check is not "ok" in the sense of "verified" -- it is "not blocking".
    ok: bool
    detail: str


class ValidationVerdict(BaseModel):
    submission_id: str
    verdict: Literal["pass", "reject"]
    checks: list[CheckResult] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    #: True only after a full pass **and** the approval gate. Validation alone
    #: never sets it; approval does.
    dispatch_ready: bool = False
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    machine: str | None = None


def _passed(check: str, detail: str) -> CheckResult:
    return CheckResult(check=check, status="pass", ok=True, detail=detail)


def _failed(check: str, detail: str) -> CheckResult:
    return CheckResult(check=check, status="fail", ok=False, detail=detail)


def _skipped(check: str, detail: str) -> CheckResult:
    return CheckResult(check=check, status="not_applicable", ok=True, detail=detail)


def _models_agree(declared: str, observed: str) -> bool:
    """Compare loosely: ``P1S`` and ``Bambu Lab P1S`` name the same machine."""

    left = declared.strip().lower()
    right = observed.strip().lower()
    return left in right or right in left


def _band_text(band: TemperatureBand) -> str:
    return f"{band.min_c:g}-{band.max_c:g} C"


def _check_machine_compatible(facts: ArtifactFacts, profile: MachineProfile) -> CheckResult:
    problems: list[str] = []
    evidence: list[str] = []

    if profile.model and facts.printer_model:
        if _models_agree(profile.model, facts.printer_model):
            evidence.append(f"sliced for {facts.printer_model}")
        else:
            problems.append(
                f"sliced for {facts.printer_model!r} but this machine is "
                f"{profile.model!r}"
            )

    if profile.nozzle_diameter_mm is not None and facts.nozzle_diameter_mm is not None:
        if abs(profile.nozzle_diameter_mm - facts.nozzle_diameter_mm) > 1e-6:
            problems.append(
                f"model needs a {facts.nozzle_diameter_mm:g} mm nozzle; this machine "
                f"has {profile.nozzle_diameter_mm:g} mm "
                f"({profile.nozzle_diameter_source})"
            )
        else:
            evidence.append(f"{facts.nozzle_diameter_mm:g} mm nozzle")

    if profile.nozzle_type and facts.nozzle_type:
        if profile.nozzle_type.strip().lower() != facts.nozzle_type.strip().lower():
            problems.append(
                f"model expects a {facts.nozzle_type} nozzle; this machine has "
                f"{profile.nozzle_type} ({profile.nozzle_type_source})"
            )
        else:
            evidence.append(f"{facts.nozzle_type} nozzle")

    if problems:
        return _failed("machine_compatible", "; ".join(problems))
    if evidence:
        return _passed("machine_compatible", "; ".join(evidence))
    return _skipped(
        "machine_compatible",
        "the model declares no printer or nozzle configuration to compare against",
    )


def _check_material_allowed(
    materials: tuple[str, ...],
    requested_material: str | None,
    profile: MachineProfile,
) -> CheckResult:
    if not materials:
        return _skipped(
            "material_allowed", "no filament type is declared by the model or the request"
        )

    forbidden = [item for item in materials if item in profile.ams.filament_forbidden]
    if forbidden:
        return _failed(
            "material_allowed",
            f"{', '.join(forbidden)} is not runnable on {profile.id}",
        )

    if requested_material:
        declared = requested_material.strip().upper()
        if declared and declared not in materials:
            return _failed(
                "material_allowed",
                f"the request declares {declared} but the model is sliced for "
                f"{', '.join(materials)}",
            )

    return _passed("material_allowed", f"{', '.join(materials)} is permitted on {profile.id}")


def _matching_trays(materials: tuple[str, ...], trays: list[LoadedTray]) -> list[LoadedTray]:
    return [
        tray
        for tray in trays
        if tray.tray_type and tray.tray_type.strip().upper() in materials
    ]


def _check_material_filament_match(
    materials: tuple[str, ...],
    trays: list[LoadedTray],
    matched: list[LoadedTray],
) -> CheckResult:
    if not trays:
        # STATUS_SPEC-conformant telemetry does not always carry AMS trays, and
        # both live printers currently report none. Recorded, never assumed.
        return _skipped(
            "material_filament_match",
            "no AMS tray inventory is reported by this printer, so the loaded "
            "filament could not be compared",
        )
    if not materials:
        return _skipped(
            "material_filament_match", "the model declares no filament type to match"
        )
    if matched:
        return _passed(
            "material_filament_match",
            f"{matched[0].label} matches the model's {', '.join(materials)}",
        )
    loaded = ", ".join(sorted({tray.tray_type or "unknown" for tray in trays}))
    return _failed(
        "material_filament_match",
        f"the model needs {', '.join(materials)} but the loaded trays hold {loaded}",
    )


def _check_nozzle_temp_in_band(
    facts: ArtifactFacts, profile: MachineProfile, matched: list[LoadedTray]
) -> CheckResult:
    temperatures = [
        ("configured", facts.nozzle_temperature_c),
        ("commanded", facts.commanded_nozzle_temperature_c),
    ]
    observed = [(label, value) for label, value in temperatures if value is not None]
    if not observed:
        return _skipped(
            "nozzle_temp_in_band", "the model declares no nozzle temperature"
        )

    problems: list[str] = []
    bands: list[str] = []

    machine_band = profile.limits.nozzle_temperature_c
    if machine_band is not None:
        bands.append(f"machine {_band_text(machine_band)}")
        for label, value in observed:
            if not machine_band.contains(value):
                problems.append(
                    f"{label} {value:g} C is outside the machine's "
                    f"{_band_text(machine_band)}"
                )

    tray = next(
        (
            item
            for item in matched
            if item.nozzle_temp_min_c is not None
            and item.nozzle_temp_max_c is not None
            # A spool tag can report nonsense; comparing against an inverted
            # window would reject every temperature, so it is ignored instead.
            and item.nozzle_temp_max_c >= item.nozzle_temp_min_c
        ),
        None,
    )
    if tray is not None:
        low = float(tray.nozzle_temp_min_c)  # type: ignore[arg-type]
        high = float(tray.nozzle_temp_max_c)  # type: ignore[arg-type]
        window = f"{low:g}-{high:g} C"
        bands.append(f"{tray.label} {window}")
        for label, value in observed:
            if not low <= value <= high:
                problems.append(
                    f"{label} {value:g} C is outside {tray.label}'s {window}"
                )

    if problems:
        return _failed("nozzle_temp_in_band", "; ".join(problems))
    if not bands:
        return _skipped(
            "nozzle_temp_in_band",
            "neither a machine nozzle limit nor a loaded filament range is known, "
            "so the nozzle temperature could not be bounded",
        )
    summary = ", ".join(f"{label} {value:g} C" for label, value in observed)
    return _passed("nozzle_temp_in_band", f"{summary} within {', '.join(bands)}")


def _check_bed_chamber_temp_in_band(
    facts: ArtifactFacts, profile: MachineProfile
) -> CheckResult:
    problems: list[str] = []
    evidence: list[str] = []

    bed_band = profile.limits.bed_temperature_c
    bed_values = [
        ("configured", facts.bed_temperature_c),
        ("commanded", facts.commanded_bed_temperature_c),
    ]
    if bed_band is not None:
        for label, value in bed_values:
            if value is None:
                continue
            if bed_band.contains(value):
                evidence.append(f"bed {label} {value:g} C within {_band_text(bed_band)}")
            else:
                problems.append(
                    f"bed {label} {value:g} C is outside {_band_text(bed_band)}"
                )

    chamber = facts.chamber_temperature_c
    if chamber is not None and chamber > 0:
        if not profile.has_chamber_control:
            problems.append(
                f"the model asks for a {chamber:g} C chamber but {profile.id} has no "
                "chamber temperature control"
            )
        else:
            chamber_band = profile.limits.chamber_temperature_c
            if chamber_band is None:
                evidence.append(
                    f"chamber {chamber:g} C requested; no machine chamber limit declared"
                )
            elif chamber_band.contains(chamber):
                evidence.append(
                    f"chamber {chamber:g} C within {_band_text(chamber_band)}"
                )
            else:
                problems.append(
                    f"chamber {chamber:g} C is outside {_band_text(chamber_band)}"
                )

    if problems:
        return _failed("bed_chamber_temp_in_band", "; ".join(problems))
    if evidence:
        return _passed("bed_chamber_temp_in_band", "; ".join(evidence))
    return _skipped(
        "bed_chamber_temp_in_band",
        "no bed or chamber limit is declared for this machine, or the model "
        "declares no bed temperature",
    )


def _check_build_fits_plate(facts: ArtifactFacts, profile: MachineProfile) -> CheckResult:
    if profile.bed_size_mm is None:
        return _skipped(
            "build_fits_plate", f"bed_size_mm is not declared for {profile.id}"
        )
    if facts.extent_mm is None:
        reason = "; ".join(facts.notes) or "the artifact reports no plate footprint"
        return _skipped("build_fits_plate", reason)

    width, depth, height = facts.extent_mm
    bed_x, bed_y = profile.bed_size_mm
    if width <= bed_x and depth <= bed_y:
        return _passed(
            "build_fits_plate",
            f"footprint {width:g} x {depth:g} mm fits the {bed_x:g} x {bed_y:g} mm plate "
            f"(height {height:g} mm)",
        )

    detail = (
        f"footprint {width:g} x {depth:g} mm exceeds the {bed_x:g} x {bed_y:g} mm plate"
    )
    if width <= bed_y and depth <= bed_x:
        # Worth saying: the fix is a re-slice with the plate rotated, not a
        # different machine.
        detail += "; it would fit rotated 90 degrees"
    return _failed("build_fits_plate", detail)


def _check_gcode_sanity(facts: ArtifactFacts) -> CheckResult:
    if not facts.sliced:
        return _skipped(
            "gcode_sanity",
            "no toolpath was found to scan; see params_present",
        )
    if facts.gcode_findings:
        detail = "; ".join(finding.detail for finding in facts.gcode_findings)
        return _failed("gcode_sanity", detail)
    caveat = (
        "heuristic scan of the leading section only (the artifact exceeded the "
        "scan budget); this is not a proof of safety"
        if facts.scan_truncated
        else "heuristic scan found no refused commands; this is not a proof of safety"
    )
    return _passed("gcode_sanity", caveat)


def _check_params_present(facts: ArtifactFacts, materials: tuple[str, ...]) -> CheckResult:
    missing: list[str] = []
    if facts.kind == "3mf" and not facts.sliced:
        missing.append("an embedded sliced plate (the printer cannot run a project file)")
    if not materials:
        missing.append("filament type")
    if facts.nozzle_temperature_c is None and facts.commanded_nozzle_temperature_c is None:
        missing.append("nozzle temperature")
    if facts.bed_temperature_c is None and facts.commanded_bed_temperature_c is None:
        missing.append("bed temperature")

    if missing:
        return _failed("params_present", f"missing {', '.join(missing)}")
    return _passed(
        "params_present", "filament type, nozzle temperature and bed temperature are declared"
    )


def validate_model(
    *,
    submission_id: str,
    facts: ArtifactFacts,
    profile: MachineProfile,
    requested_material: str | None = None,
) -> ValidationVerdict:
    """Run every check and return the machine-readable verdict."""

    materials = facts.filament_types
    if not materials and requested_material:
        materials = (requested_material.strip().upper(),)

    trays = list(profile.observed.loaded_trays)
    matched = _matching_trays(materials, trays)

    checks = [
        _check_machine_compatible(facts, profile),
        _check_material_allowed(materials, requested_material, profile),
        _check_material_filament_match(materials, trays, matched),
        _check_nozzle_temp_in_band(facts, profile, matched),
        _check_bed_chamber_temp_in_band(facts, profile),
        _check_build_fits_plate(facts, profile),
        _check_gcode_sanity(facts),
        _check_params_present(facts, materials),
    ]
    # The declared order is part of the contract: readers render it as a
    # checklist and the order should not drift with the code.
    checks.sort(key=lambda result: CHECK_ORDER.index(result.check))

    reasons = [result.check for result in checks if result.status == "fail"]
    return ValidationVerdict(
        submission_id=submission_id,
        verdict="reject" if reasons else "pass",
        checks=checks,
        reasons=reasons,
        dispatch_ready=False,
        machine=profile.id,
    )
