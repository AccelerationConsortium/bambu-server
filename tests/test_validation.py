"""Model validation against a machine profile (bambu_server.validation)."""

from __future__ import annotations

from bambu_server.artifacts import ArtifactFacts, GcodeFinding
from bambu_server.config import MachineProfileConfig, PrinterDefinition
from bambu_server.profiles import LoadedTray, ObservedMachineState, build_profile
from bambu_server.validation import ValidationVerdict, validate_model


def _profile(**overrides: object):
    declared = {
        "enclosure": "enclosed",
        "nozzle_type": "hardened_steel",
        "nozzle_diameter_mm": 0.4,
        "bed_size_mm": (256.0, 256.0),
        "limits": {
            "nozzle_temperature_c": [0, 300],
            "bed_temperature_c": [0, 110],
        },
        "ams": {"filament_forbidden": ["ABS"]},
    }
    declared.update(overrides.pop("declared", {}))  # type: ignore[arg-type]
    definition = PrinterDefinition(
        id="bambu_test_01",
        name="Bambu Test 01",
        model="X1 Carbon",
        env_prefix="BAMBU_TEST_01",
        profile=MachineProfileConfig.model_validate(declared),
    )
    observed = overrides.pop("observed", ObservedMachineState(telemetry_ok=True))
    return build_profile(definition, observed)  # type: ignore[arg-type]


def _facts(**overrides: object) -> ArtifactFacts:
    base: dict[str, object] = {
        "kind": "gcode",
        "byte_size": 1024,
        "sliced": True,
        "filament_types": ("PLA",),
        "nozzle_temperature_c": 220.0,
        "bed_temperature_c": 55.0,
        "chamber_temperature_c": 0.0,
        "nozzle_diameter_mm": 0.4,
        "nozzle_type": "hardened_steel",
        "printer_model": "Bambu Lab X1 Carbon",
        "extent_mm": (100.0, 50.0, 30.0),
        "extent_source": "gcode_motion",
        "estimated_duration_minutes": 70.0,
    }
    base.update(overrides)
    return ArtifactFacts.model_validate(base)


def _run(facts: ArtifactFacts, profile=None, material: str | None = None) -> ValidationVerdict:
    return validate_model(
        submission_id="a" * 32,
        facts=facts,
        profile=profile or _profile(),
        requested_material=material,
    )


def _check(verdict: ValidationVerdict, name: str):
    return next(result for result in verdict.checks if result.check == name)


def test_a_matching_model_passes_every_applicable_check() -> None:
    verdict = _run(_facts())

    assert verdict.verdict == "pass"
    assert verdict.reasons == []
    # Passing validation is not the same as being cleared to run.
    assert verdict.dispatch_ready is False
    assert _check(verdict, "machine_compatible").status == "pass"
    assert _check(verdict, "build_fits_plate").status == "pass"


def test_checks_are_reported_in_the_declared_order() -> None:
    verdict = _run(_facts())
    assert [result.check for result in verdict.checks] == [
        "machine_compatible",
        "material_allowed",
        "material_filament_match",
        "nozzle_temp_in_band",
        "bed_chamber_temp_in_band",
        "build_fits_plate",
        "gcode_sanity",
        "params_present",
    ]


def test_a_single_failing_check_rejects_the_submission() -> None:
    verdict = _run(_facts(nozzle_diameter_mm=0.6))

    assert verdict.verdict == "reject"
    assert verdict.reasons == ["machine_compatible"]
    assert "0.6 mm nozzle" in _check(verdict, "machine_compatible").detail


def test_a_model_sliced_for_another_printer_is_rejected() -> None:
    verdict = _run(_facts(printer_model="Bambu Lab P1S"))

    assert verdict.reasons == ["machine_compatible"]


def test_loose_model_matching_accepts_a_qualified_name() -> None:
    """`X1 Carbon` and `Bambu Lab X1 Carbon` name the same machine."""
    assert _run(_facts(printer_model="Bambu Lab X1 Carbon")).verdict == "pass"


def test_a_forbidden_material_is_rejected() -> None:
    verdict = _run(_facts(filament_types=("ABS",)))

    assert "material_allowed" in verdict.reasons
    assert "not runnable" in _check(verdict, "material_allowed").detail


def test_a_declared_material_must_match_the_sliced_one() -> None:
    verdict = _run(_facts(), material="PETG")

    assert verdict.reasons == ["material_allowed"]
    assert "sliced for PLA" in _check(verdict, "material_allowed").detail


def test_missing_tray_data_is_not_applicable_rather_than_a_pass() -> None:
    """Both live printers report no AMS trays; that must not read as verified."""
    result = _check(_run(_facts()), "material_filament_match")

    assert result.status == "not_applicable"
    assert result.ok is True
    assert "no AMS tray inventory" in result.detail


def test_a_loaded_tray_that_does_not_match_is_rejected() -> None:
    profile = _profile(
        observed=ObservedMachineState(
            telemetry_ok=True,
            loaded_trays=[LoadedTray(ams_id=0, tray_id=1, tray_type="PETG")],
        )
    )
    verdict = _run(_facts(), profile)

    assert "material_filament_match" in verdict.reasons
    assert "loaded trays hold PETG" in _check(verdict, "material_filament_match").detail


def test_nozzle_temperature_is_checked_against_the_loaded_filament() -> None:
    profile = _profile(
        observed=ObservedMachineState(
            telemetry_ok=True,
            loaded_trays=[
                LoadedTray(
                    ams_id=0,
                    tray_id=1,
                    tray_type="PLA",
                    nozzle_temp_min_c=190,
                    nozzle_temp_max_c=240,
                )
            ],
        )
    )

    assert _run(_facts(), profile).verdict == "pass"

    hot = _run(_facts(nozzle_temperature_c=260.0), profile)
    assert hot.reasons == ["nozzle_temp_in_band"]
    assert "outside" in _check(hot, "nozzle_temp_in_band").detail


def test_a_commanded_temperature_above_the_machine_limit_is_rejected() -> None:
    """An edited toolpath can command more than its own configured setpoint."""
    verdict = _run(_facts(commanded_nozzle_temperature_c=400.0))

    assert verdict.reasons == ["nozzle_temp_in_band"]
    assert "commanded 400 C" in _check(verdict, "nozzle_temp_in_band").detail


def test_a_chamber_request_needs_a_machine_with_a_chamber() -> None:
    verdict = _run(_facts(chamber_temperature_c=50.0))

    assert verdict.reasons == ["bed_chamber_temp_in_band"]
    assert "no chamber temperature control" in _check(
        verdict, "bed_chamber_temp_in_band"
    ).detail


def test_a_chamber_request_passes_on_a_machine_that_has_one() -> None:
    profile = _profile(
        declared={"chamber_temperature_c": 60.0, "limits": {"chamber_temperature_c": [0, 65]}}
    )
    assert _run(_facts(chamber_temperature_c=50.0), profile).verdict == "pass"


def test_an_oversized_model_is_rejected_with_a_rotation_hint() -> None:
    verdict = _run(_facts(extent_mm=(300.0, 100.0, 20.0)))

    assert verdict.reasons == ["build_fits_plate"]
    assert "exceeds" in _check(verdict, "build_fits_plate").detail

    profile = _profile(declared={"bed_size_mm": (150.0, 350.0)})
    rotated = _run(_facts(extent_mm=(300.0, 100.0, 20.0)), profile)
    assert "fit rotated" in _check(rotated, "build_fits_plate").detail


def test_plate_fit_is_not_applicable_without_a_declared_bed() -> None:
    profile = _profile(declared={"bed_size_mm": None})
    result = _check(_run(_facts(), profile), "build_fits_plate")

    assert result.status == "not_applicable"
    assert "bed_size_mm is not declared" in result.detail


def test_gcode_findings_reject_and_the_pass_does_not_overclaim() -> None:
    verdict = _run(
        _facts(gcode_findings=(GcodeFinding(code="forbidden_command", detail="M997 bad"),))
    )
    assert verdict.reasons == ["gcode_sanity"]

    clean = _check(_run(_facts()), "gcode_sanity")
    assert clean.status == "pass"
    assert "not a proof of safety" in clean.detail


def test_missing_print_settings_are_rejected() -> None:
    verdict = _run(
        _facts(filament_types=(), nozzle_temperature_c=None, bed_temperature_c=None)
    )

    detail = _check(verdict, "params_present").detail
    assert "params_present" in verdict.reasons
    assert "filament type" in detail
    assert "nozzle temperature" in detail
    assert "bed temperature" in detail


def test_an_unsliced_project_file_cannot_be_run() -> None:
    verdict = _run(_facts(kind="3mf", sliced=False, extent_mm=None))

    assert "params_present" in verdict.reasons
    assert "embedded sliced plate" in _check(verdict, "params_present").detail
    # Nothing to scan, so the sanity check declines rather than passing.
    assert _check(verdict, "gcode_sanity").status == "not_applicable"


def test_a_degenerate_tray_window_is_handled_not_crashed() -> None:
    """A spool tag can report min == max; a single-point window still checks."""
    profile = _profile(
        observed=ObservedMachineState(
            telemetry_ok=True,
            loaded_trays=[
                LoadedTray(
                    ams_id=0,
                    tray_id=1,
                    tray_type="PLA",
                    nozzle_temp_min_c=220,
                    nozzle_temp_max_c=220,
                )
            ],
        )
    )

    assert _run(_facts(), profile).verdict == "pass"
    assert _run(_facts(nozzle_temperature_c=225.0), profile).reasons == [
        "nozzle_temp_in_band"
    ]


def test_an_inverted_tray_window_is_ignored_rather_than_rejecting_everything() -> None:
    profile = _profile(
        observed=ObservedMachineState(
            telemetry_ok=True,
            loaded_trays=[
                LoadedTray(
                    ams_id=0,
                    tray_id=1,
                    tray_type="PLA",
                    nozzle_temp_min_c=240,
                    nozzle_temp_max_c=190,
                )
            ],
        )
    )
    verdict = _run(_facts(), profile)

    assert verdict.verdict == "pass"
    # The machine limit still applies; only the nonsense spool window is dropped.
    assert "machine 0-300 C" in _check(verdict, "nozzle_temp_in_band").detail
