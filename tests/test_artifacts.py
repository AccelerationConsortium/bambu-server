"""Artifact inspection (bambu_server.artifacts)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from bambu_server.artifacts import (
    ArtifactError,
    inspect_artifact,
    parse_duration_minutes,
)

from .conftest import SAMPLE_GCODE, write_3mf, write_gcode

SCAN_BUDGET = 1 << 20


def _inspect(path: Path, budget: int = SCAN_BUDGET):
    return inspect_artifact(path, scan_max_bytes=budget)


def test_gcode_settings_and_footprint(tmp_path: Path) -> None:
    facts = _inspect(write_gcode(tmp_path / "part.gcode"))

    assert facts.kind == "gcode"
    assert facts.sliced is True
    assert facts.filament_types == ("PLA",)
    assert facts.nozzle_temperature_c == 220
    assert facts.nozzle_diameter_mm == 0.4
    assert facts.nozzle_type == "hardened_steel"
    assert facts.layer_height_mm == 0.2
    assert facts.printer_model == "Bambu Lab X1 Carbon"
    # X 10..110 and Y 10..60 -- an extent, not a position, so where the slicer
    # placed the part on the plate cannot change the answer.
    assert facts.extent_mm == (100.0, 50.0, 0.0)
    assert facts.extent_source == "gcode_motion"
    assert facts.gcode_findings == ()


def test_bed_temperature_follows_the_selected_plate(tmp_path: Path) -> None:
    """`curr_bed_type` picks which plate temperature the job actually uses."""
    facts = _inspect(write_gcode(tmp_path / "part.gcode"))

    # Textured plate is selected, so 55 -- not the 60 declared for the hot plate.
    assert facts.bed_temperature_c == 55


def test_commanded_temperatures_are_tracked_separately(tmp_path: Path) -> None:
    """A temperature command can exceed the configured setpoint after an edit."""
    body = SAMPLE_GCODE.replace("M104 S220", "M104 S400")
    facts = _inspect(write_gcode(tmp_path / "edited.gcode", body))

    assert facts.nozzle_temperature_c == 220
    assert facts.commanded_nozzle_temperature_c == 400


def test_total_estimated_time_is_preferred_over_model_time(tmp_path: Path) -> None:
    facts = _inspect(write_gcode(tmp_path / "part.gcode"))
    assert facts.estimated_duration_minutes == 70.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1h 10m 0s", 70.0),
        ("45s", 0.75),
        ("2m 30s", 2.5),
        ("4200", 70.0),
        ("", None),
        ("not a time", None),
    ],
)
def test_duration_parsing(text: str, expected: float | None) -> None:
    assert parse_duration_minutes(text) == expected


def test_forbidden_command_is_reported(tmp_path: Path) -> None:
    body = SAMPLE_GCODE + "M997\nM502\n"
    facts = _inspect(write_gcode(tmp_path / "risky.gcode", body))

    codes = {finding.code for finding in facts.gcode_findings}
    details = " ".join(finding.detail for finding in facts.gcode_findings)
    assert codes == {"forbidden_command"}
    assert "M997" in details and "M502" in details


def test_relative_positioning_withholds_the_footprint(tmp_path: Path) -> None:
    body = SAMPLE_GCODE + "G91\nG1 X5 Y5\n"
    facts = _inspect(write_gcode(tmp_path / "relative.gcode", body))

    assert facts.extent_mm is None
    assert any("relative positioning" in note for note in facts.notes)


def test_truncated_scan_withholds_the_footprint(tmp_path: Path) -> None:
    """A partly-read toolpath must not be reported as a whole one."""
    body = SAMPLE_GCODE + "".join(f"G1 X{i % 200} Y{i % 200}\n" for i in range(20000))
    facts = _inspect(write_gcode(tmp_path / "big.gcode", body), budget=4096)

    assert facts.scan_truncated is True
    assert facts.extent_mm is None
    # The head-and-tail read still recovers the slicer's settings.
    assert facts.nozzle_temperature_c == 220


def test_non_gcode_content_is_not_sliced(tmp_path: Path) -> None:
    facts = _inspect(write_gcode(tmp_path / "junk.gcode", "hello\nworld\n"))

    assert facts.sliced is False
    assert any("no gcode commands" in note for note in facts.notes)


def test_sliced_3mf_reads_its_embedded_plate(tmp_path: Path) -> None:
    facts = _inspect(write_3mf(tmp_path / "plate.3mf"))

    assert facts.kind == "3mf"
    assert facts.sliced is True
    assert facts.filament_types == ("PLA",)
    assert facts.nozzle_temperature_c == 220
    assert facts.bed_temperature_c == 55
    assert facts.extent_mm == (100.0, 50.0, 0.0)
    assert facts.extent_source == "embedded_plate_gcode"
    # The embedded gcode's own header wins over slice_info's `prediction`.
    assert facts.estimated_duration_minutes == 70.0


def test_unsliced_3mf_is_reported_as_unrunnable(tmp_path: Path) -> None:
    facts = _inspect(write_3mf(tmp_path / "project.3mf", plate_gcode=None))

    assert facts.sliced is False
    assert facts.extent_mm is None
    assert any("no sliced plate gcode" in note for note in facts.notes)
    # The sidecar settings are still read, so the reason for rejection is the
    # missing toolpath and not a pile of missing parameters.
    assert facts.nozzle_temperature_c == 220
    assert facts.filament_types == ("PLA",)


def test_unsliced_3mf_falls_back_to_slice_info_duration(tmp_path: Path) -> None:
    facts = _inspect(write_3mf(tmp_path / "project.3mf", plate_gcode=None))
    assert facts.estimated_duration_minutes == 70.0


def test_xml_with_a_doctype_is_refused(tmp_path: Path) -> None:
    """A DOCTYPE is where an entity expansion would be declared, so it is refused."""
    hostile = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE config [<!ENTITY a "aaaaaaaaaa">]>\n'
        "<config><plate>&a;</plate></config>\n"
    )
    facts = _inspect(
        write_3mf(tmp_path / "hostile.3mf", plate_gcode=None, slice_info=hostile)
    )

    # The document is dropped whole rather than parsed; the project settings
    # still answer for the fields slice_info would have.
    assert facts.nozzle_temperature_c == 220


def test_multiple_plates_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "two_plates.3mf"
    write_3mf(path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Metadata/plate_2.gcode", SAMPLE_GCODE)

    facts = _inspect(path)
    assert any("2 plates" in note for note in facts.notes)


def test_unsupported_extension_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "model.stl"
    path.write_bytes(b"solid\n")
    with pytest.raises(ArtifactError):
        _inspect(path)
