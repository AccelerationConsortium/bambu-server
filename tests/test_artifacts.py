"""Artifact inspection (bambu_server.artifacts)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from bambu_server.artifacts import (
    ArtifactError,
    inspect_artifact,
    inspect_object_positions,
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


# Bambu's stock A1 machine start gcode, cut down to the bed-leveling step.
STOCK_MESH_SAVE = """M622 J1
    M1002 gcode_claim_action : 1
    G29 A1 X117.7 Y38.9 I43.2 J10.2
    M400
    M500 ; save cali data
M623
"""
START_END = "; MACHINE_START_GCODE_END\n"


def _forbidden(facts) -> list[str]:
    return [f.detail for f in facts.gcode_findings if f.code == "forbidden_command"]


def test_the_stock_bed_mesh_save_in_the_start_gcode_is_accepted(tmp_path: Path) -> None:
    body = STOCK_MESH_SAVE + START_END + SAMPLE_GCODE
    facts = _inspect(write_gcode(tmp_path / "a1.gcode", body))
    assert _forbidden(facts) == []


@pytest.mark.parametrize(
    "body",
    [
        # No probe right before it.
        "G28\nM500\n" + START_END + SAMPLE_GCODE,
        # Something other than a wait between the probe and the save.
        "G29 A1\nG1 Z5\nM500\n" + START_END + SAMPLE_GCODE,
        # After the start gcode has ended.
        START_END + "G29 A1\nM400\nM500\n" + SAMPLE_GCODE,
        # The start gcode's end marker never appears.
        STOCK_MESH_SAVE + SAMPLE_GCODE,
    ],
    ids=["no-probe", "not-adjacent", "after-start", "no-end-marker"],
)
def test_any_other_m500_is_still_refused(tmp_path: Path, body: str) -> None:
    facts = _inspect(write_gcode(tmp_path / "risky.gcode", body))
    assert any("M500" in detail for detail in _forbidden(facts))


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


WIDGET_MODEL = """<?xml version="1.0"?>
<model><resources><object id="7" name="widget"><mesh><vertices>
<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
<vertex x="10" y="20" z="0"/></vertices></mesh></object></resources>
<build><item objectid="7" transform="1 0 0 0 1 0 0 0 1 30 40 0"/></build></model>"""

# Bambu Studio brackets each object's toolpath with unique label ids.
BAMBU_LABELLED = SAMPLE_GCODE + (
    "; start printing object, unique label id: 101\n"
    "G1 X20 Y30 F600\nG1 X40 Y50 E10\n"
    "; stop printing object, unique label id: 101\n"
    "G1 X200 Y200 F9000\n"  # travel between objects is not part of either
    "; start printing object, unique label id: 102\n"
    "G1 X100 Y100 F600\nG1 X120 Y110 E12\n"
    "; stop printing object, unique label id: 102\n"
)


def test_slicer_plate_json_gives_bed_positions_cross_checked_by_gcode(tmp_path: Path) -> None:
    plate_json = json.dumps({"bbox_objects": [
        {"id": 101, "name": "bracket", "bbox": [18.0, 28.0, 42.0, 52.0]},
        {"id": 102, "name": "clip", "bbox": [98.0, 98.0, 122.0, 112.0]},
    ]})
    path = write_3mf(
        tmp_path / "plate.3mf",
        plate_gcode=BAMBU_LABELLED,
        extra_members={"Metadata/plate_1.json": plate_json},
    )

    objects = inspect_object_positions(path, scan_max_bytes=SCAN_BUDGET)

    assert [o.name for o in objects] == ["bracket", "clip"]
    bracket = objects[0]
    assert bracket.source == "slicer_plate_json"
    assert bracket.plate_index == 1
    assert (bracket.center_x_mm, bracket.center_y_mm) == (30.0, 40.0)
    assert bracket.bounds.max_x_mm == 42.0
    # The extrusion inside label 101 only, not the travel move to X200.
    assert bracket.gcode_bounds is not None
    assert (bracket.gcode_bounds.min_x_mm, bracket.gcode_bounds.max_x_mm) == (20.0, 40.0)
    assert objects[1].gcode_bounds.max_y_mm == 110.0


def test_bambu_gcode_labels_are_used_without_plate_json(tmp_path: Path) -> None:
    path = write_3mf(tmp_path / "plate.3mf", plate_gcode=BAMBU_LABELLED)

    objects = inspect_object_positions(path, scan_max_bytes=SCAN_BUDGET)

    assert [(o.object_id, o.source) for o in objects] == [
        ("101", "gcode_label"), ("102", "gcode_label"),
    ]
    assert objects[0].bounds.max_x_mm == 40.0  # no travel to X200


def test_model_transform_is_the_last_resort_on_plate_one(tmp_path: Path) -> None:
    path = write_3mf(tmp_path / "objects.3mf", model_xml=WIDGET_MODEL)

    objects = inspect_object_positions(path, scan_max_bytes=SCAN_BUDGET)

    assert len(objects) == 1
    assert objects[0].source == "model_transform"
    assert objects[0].name == "widget"
    assert (objects[0].center_x_mm, objects[0].center_y_mm) == (35.0, 50.0)
    assert objects[0].rotation_deg == 0.0


def test_model_transforms_are_not_trusted_for_later_plates(tmp_path: Path) -> None:
    """Later plates are offset in the project layout, not in bed coordinates."""
    path = write_3mf(tmp_path / "plate2.3mf", plate_gcode=None, model_xml=WIDGET_MODEL)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Metadata/plate_2.gcode", SAMPLE_GCODE)

    assert inspect_object_positions(path, scan_max_bytes=SCAN_BUDGET) == ()
