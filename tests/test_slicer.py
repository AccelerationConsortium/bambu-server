"""Slicing an uploaded mesh, with a stand-in for the Bambu Studio CLI.

The stand-in is a small script that behaves like the real CLI's file contract
(read settings, write ``<outputdir>/<name>.3mf`` and ``result.json``), so these
tests need no Bambu Studio install and never run a real slicer.
"""

from __future__ import annotations

import json
import stat
import struct
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from bambu_server.config import Settings, SlicerSettings
from bambu_server.main import create_app
from bambu_server.slicer import BambuStudioSlicer, SliceRequest, SlicerError, SlicerUnavailable

from .conftest import SAMPLE_GCODE, FakeBackend, write_3mf

FAKE_CLI = """#!{python}
import json, shutil, sys
from pathlib import Path
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1]
out = Path(value("--outputdir"))
settings = value("--load-settings").split(";")
(out / "seen_settings.json").write_text(json.dumps(settings))
(out / "seen_argv.json").write_text(json.dumps(args))
if Path(args[-1]).read_bytes().startswith(b"broken"):
    (out / "result.json").write_text(json.dumps({{"return_code": -51, "error_string": "Nothing to be sliced"}}))
    sys.exit(0)
shutil.copy({template!r}, out / value("--export-3mf"))
(out / "result.json").write_text(json.dumps({{
    "return_code": 0,
    "error_string": "Success.",
    "sliced_plates": [{{"total_predication": 612.0, "warning_message": ""}}],
}}))
"""


def _write(root: Path, kind: str, body: dict[str, object]) -> None:
    (root / kind).mkdir(parents=True, exist_ok=True)
    (root / kind / f"{body['name']}.json").write_text(json.dumps(body))


def _profiles(root: Path) -> Path:
    # Shaped like the bundled BBL tree: the instantiable preset holds only its
    # own keys; the bed, gcode and temperatures live in parents and includes.
    _write(root, "machine", {
        "name": "fdm_machine_test_common", "printable_area": ["0x0", "256x0", "256x256", "0x256"],
        "retraction_length": ["0.8"],
        "machine_end_gcode": "G1 X0 Y128 F18000\nM104 S0\nG90\nG1 X-48 Y180 F3600\nM400\nM18 X Y Z\n",
    })
    _write(root, "machine", {"name": "Bambu Lab Test 0.4 nozzle template machine_start_gcode", "instantiation": "false", "machine_start_gcode": ";===== machine: Test ====="})
    _write(root, "machine", {
        "name": "Bambu Lab Test 0.4 nozzle", "inherits": "fdm_machine_test_common",
        "include": ["Bambu Lab Test 0.4 nozzle template machine_start_gcode"],
        "nozzle_type": ["stainless_steel"], "retraction_length": ["0.5"],
    })
    _write(root, "process", {"name": "fdm_process_test_common", "layer_height": "0.2", "wall_loops": "2"})
    _write(root, "process", {"name": "0.20mm Standard @BBL X1C", "inherits": "fdm_process_test_common", "wall_loops": "3"})
    _write(root, "filament", {"name": "fdm_filament_test_pla", "nozzle_temperature": ["220"]})
    _write(root, "filament", {"name": "Bambu PLA Basic @BBL Test", "inherits": "fdm_filament_test_pla"})
    return root


@pytest.fixture
def slicer_settings(tmp_path: Path) -> SlicerSettings:
    template = write_3mf(tmp_path / "template.3mf")
    cli = tmp_path / "fake-bambu-studio"
    cli.write_text(FAKE_CLI.format(python=sys.executable, template=str(template)))
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return SlicerSettings(
        enabled=True,
        executable=cli,
        profiles_dir=_profiles(tmp_path / "profiles"),
        work_dir=tmp_path / "work",
        filament_profiles={"pla": "Bambu PLA Basic @BBL Test"},
        machine_profiles={"bambu_test_01": "Bambu Lab Test 0.4 nozzle"},
    )


def _request(**overrides: object) -> SliceRequest:
    values: dict[str, object] = {
        "printer_id": "bambu_test_01",
        "material": "PLA",
        "nozzle_type": "hardened_steel",
        "plate": "textured_plate",
    }
    values.update(overrides)
    return SliceRequest(**values)  # type: ignore[arg-type]


async def test_slice_forces_the_machines_nozzle_and_plate(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid cube\nendsolid cube\n")
    slicer = BambuStudioSlicer(slicer_settings)
    job_dir = slicer_settings.work_dir / "job"

    result = await slicer.slice(mesh, _request(), job_dir=job_dir)

    assert result.artifact.is_file()
    assert result.estimated_minutes == pytest.approx(10.2)
    machine = json.loads((job_dir / "machine.json").read_text())
    assert machine["nozzle_type"] == ["hardened_steel"]
    assert machine["curr_bed_type"] == "Textured PEI Plate"
    seen = json.loads((job_dir / "seen_settings.json").read_text())
    assert seen[0].endswith("machine.json")
    assert seen[1].endswith("process.json")


async def test_presets_are_flattened_before_the_cli_sees_them(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    """The CLI ignores ``inherits``/``include`` it cannot resolve; until this was
    fixed an A1 slice silently got a 200 x 200 bed and a generic G28 start."""

    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid cube\nendsolid cube\n")
    job_dir = slicer_settings.work_dir / "job"
    await BambuStudioSlicer(slicer_settings).slice(mesh, _request(), job_dir=job_dir)

    machine = json.loads((job_dir / "machine.json").read_text())
    assert "inherits" not in machine and "include" not in machine
    assert machine["printable_area"] == ["0x0", "256x0", "256x256", "0x256"]  # from the parent
    assert machine["machine_start_gcode"].startswith(";===== machine: Test")  # from the include
    assert machine["retraction_length"] == ["0.5"]  # the child's own value wins
    assert machine["name"] == "Bambu Lab Test 0.4 nozzle"
    process = json.loads((job_dir / "process.json").read_text())
    assert (process["layer_height"], process["wall_loops"]) == ("0.2", "3")
    filament = json.loads((job_dir / "filament.json").read_text())
    assert filament["nozzle_temperature"] == ["220"]


async def test_an_inherits_loop_is_refused(slicer_settings: SlicerSettings, tmp_path: Path) -> None:
    root = slicer_settings.profiles_dir
    _write(root, "process", {"name": "0.20mm Standard @BBL X1C", "inherits": "loop_b"})
    _write(root, "process", {"name": "loop_b", "inherits": "0.20mm Standard @BBL X1C"})
    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid cube\nendsolid cube\n")
    with pytest.raises(SlicerUnavailable, match="inherits loop"):
        await BambuStudioSlicer(slicer_settings).slice(
            mesh, _request(), job_dir=slicer_settings.work_dir / "job"
        )


async def test_a_slicer_failure_is_reported(slicer_settings: SlicerSettings, tmp_path: Path) -> None:
    mesh = tmp_path / "broken.stl"
    mesh.write_text("broken mesh")
    with pytest.raises(SlicerError, match="Nothing to be sliced"):
        await BambuStudioSlicer(slicer_settings).slice(
            mesh, _request(), job_dir=slicer_settings.work_dir / "x"
        )


def _cube_stl(origin: tuple[float, float, float], size: tuple[float, float, float]) -> bytes:
    """A binary STL box with one corner at ``origin``."""

    corners = [
        (origin[0] + dx * size[0], origin[1] + dy * size[1], origin[2] + dz * size[2])
        for dx in (0, 1) for dy in (0, 1) for dz in (0, 1)
    ]
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    body = b"".join(
        struct.pack("<3f", 0, 0, 0)
        + b"".join(struct.pack("<3f", *corners[i]) for i in face)
        + b"\0\0"
        for face in faces
    )
    return b"\0" * 80 + struct.pack("<I", len(faces)) + body


def _placed_bounds(model_3mf: Path) -> tuple[float, float, float, float, float, int]:
    """(min_x, min_y, max_x, max_y, min_z, vertex count) of the build item, transformed."""

    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    with zipfile.ZipFile(model_3mf) as archive:
        root = ElementTree.fromstring(archive.read("3D/3dmodel.model"))
    vertices = [
        (float(v.get("x")), float(v.get("y")), float(v.get("z")))
        for v in root.iterfind(".//m:vertex", ns)
    ]
    transform = [float(t) for t in root.find(".//m:item", ns).get("transform").split()]
    assert transform[:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1]  # translated, never rotated
    tx, ty, tz = transform[9:]
    xs = [x + tx for x, _, _ in vertices]
    ys = [y + ty for _, y, _ in vertices]
    zs = [z + tz for _, _, z in vertices]
    return min(xs), min(ys), max(xs), max(ys), min(zs), len(vertices)


async def test_front_left_places_the_part_a_margin_from_the_bed_origin(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "cube.stl"
    mesh.write_bytes(_cube_stl((100.0, 150.0, 5.0), (20.0, 30.0, 10.0)))
    job_dir = slicer_settings.work_dir / "job"

    result = await BambuStudioSlicer(slicer_settings).slice(
        mesh, _request(placement="front_left"), job_dir=job_dir
    )

    argv = json.loads((job_dir / "seen_argv.json").read_text())
    assert argv[argv.index("--arrange") + 1] == "0"  # arranging would re-centre it
    assert argv[-1].endswith("placed.3mf")
    min_x, min_y, max_x, max_y, min_z, count = _placed_bounds(job_dir / "placed.3mf")
    margin = slicer_settings.placement_margin_mm
    assert (min_x, min_y) == pytest.approx((margin, margin))
    assert (max_x, max_y) == pytest.approx((margin + 20.0, margin + 30.0))
    assert min_z == pytest.approx(0.0)  # rests on the plate
    assert count == 8  # shared vertices: one connected mesh, not loose triangles
    assert result.placed_min_xy_mm == (margin, margin)


async def test_front_left_reads_an_ascii_stl(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "tri.stl"
    mesh.write_text(
        "solid t\nfacet normal 0 0 1\nouter loop\n"
        "vertex -50 -40 2\nvertex -30 -40 2\nvertex -50 -20 7\n"
        "endloop\nendfacet\nendsolid t\n"
    )
    job_dir = slicer_settings.work_dir / "job"
    settings = slicer_settings.model_copy(update={"placement_margin_mm": 5.0})
    await BambuStudioSlicer(settings).slice(mesh, _request(placement="front_left"), job_dir=job_dir)

    min_x, min_y, max_x, max_y, min_z, _ = _placed_bounds(job_dir / "placed.3mf")
    assert (min_x, min_y, max_x, max_y, min_z) == pytest.approx((5.0, 5.0, 25.0, 25.0, 0.0))


async def test_auto_placement_lets_the_slicer_arrange_the_stl(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "cube.stl"
    mesh.write_bytes(_cube_stl((0.0, 0.0, 0.0), (10.0, 10.0, 10.0)))
    job_dir = slicer_settings.work_dir / "job"
    result = await BambuStudioSlicer(slicer_settings).slice(mesh, _request(), job_dir=job_dir)

    argv = json.loads((job_dir / "seen_argv.json").read_text())
    assert argv[argv.index("--arrange") + 1] == "1"
    assert argv[-1] == str(mesh)
    assert result.placed_min_xy_mm is None


@pytest.mark.parametrize(
    ("stl", "reason"),
    [
        (_cube_stl((0.0, 0.0, 0.0), (250.0, 20.0, 10.0)), "does not fit"),
        (b"not a mesh at all", "not a readable STL"),
        (b"solid empty\nendsolid empty\n", "no triangles"),
    ],
)
async def test_front_left_refuses_a_part_it_cannot_place(
    slicer_settings: SlicerSettings, tmp_path: Path, stl: bytes, reason: str
) -> None:
    mesh = tmp_path / "part.stl"
    mesh.write_bytes(stl)
    job_dir = slicer_settings.work_dir / "job"
    with pytest.raises(SlicerError, match=reason):
        await BambuStudioSlicer(slicer_settings).slice(
            mesh, _request(placement="front_left"), job_dir=job_dir
        )
    assert not (job_dir / "result.json").exists()  # the CLI never ran


def _with_park(settings: SlicerSettings, y_mm: float) -> SlicerSettings:
    payload = settings.model_dump(mode="json")
    payload["machine_profiles"]["bambu_test_01"]["end_bed_y_mm"] = y_mm
    return SlicerSettings.model_validate(payload)


async def test_end_bed_y_moves_only_the_final_park_move(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid cube\nendsolid cube\n")
    job_dir = slicer_settings.work_dir / "job"
    result = await BambuStudioSlicer(_with_park(slicer_settings, 250)).slice(
        mesh, _request(), job_dir=job_dir
    )

    end = json.loads((job_dir / "machine.json").read_text())["machine_end_gcode"]
    assert "G1 X-48 Y250 F3600 ; bed parked for pickup" in end
    assert "Y180" not in end
    assert "G1 X0 Y128 F18000" in end  # earlier moves are untouched
    assert end.index("Y250") < end.index("M18")  # parked before the motors are released
    assert result.end_bed_y_mm == 250


async def test_stock_end_gcode_is_kept_without_end_bed_y(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid cube\nendsolid cube\n")
    job_dir = slicer_settings.work_dir / "job"
    result = await BambuStudioSlicer(slicer_settings).slice(mesh, _request(), job_dir=job_dir)

    end = json.loads((job_dir / "machine.json").read_text())["machine_end_gcode"]
    assert "G1 X-48 Y180 F3600\n" in end
    assert result.end_bed_y_mm is None


async def test_end_bed_y_beyond_the_bed_is_refused(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid cube\nendsolid cube\n")
    with pytest.raises(SlicerUnavailable, match="outside the bed"):
        await BambuStudioSlicer(_with_park(slicer_settings, 300)).slice(
            mesh, _request(), job_dir=slicer_settings.work_dir / "job"
        )


async def test_end_bed_y_without_a_park_move_is_refused(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    root = slicer_settings.profiles_dir
    common = root / "machine" / "fdm_machine_test_common.json"
    body = json.loads(common.read_text())
    body["machine_end_gcode"] = "M104 S0\nM18 X Y Z\n"
    common.write_text(json.dumps(body))
    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid cube\nendsolid cube\n")
    with pytest.raises(SlicerUnavailable, match="no final park move"):
        await BambuStudioSlicer(_with_park(slicer_settings, 250)).slice(
            mesh, _request(), job_dir=slicer_settings.work_dir / "job"
        )


def test_unknown_material_and_unknown_printer_are_refused(
    slicer_settings: SlicerSettings,
) -> None:
    slicer = BambuStudioSlicer(slicer_settings)
    with pytest.raises(SlicerError, match="PLA"):
        slicer.resolve_presets(_request(material="PEEK"))
    with pytest.raises(SlicerUnavailable):
        slicer.resolve_presets(_request(printer_id="bambu_other"))
    assert not slicer.accepts("bambu_other")


async def test_slicing_without_a_declared_plate_is_refused(
    slicer_settings: SlicerSettings, tmp_path: Path
) -> None:
    mesh = tmp_path / "cube.stl"
    mesh.write_text("solid")
    with pytest.raises(SlicerError, match="build plate"):
        await BambuStudioSlicer(slicer_settings).slice(
            mesh, _request(plate=None), job_dir=slicer_settings.work_dir / "y"
        )


def _preset(root: Path, kind: str, name: str, compatible: list[str] | None = None) -> None:
    body: dict[str, object] = {"name": name}
    if compatible is not None:
        body["compatible_printers"] = compatible
    (root / kind / f"{name}.json").write_text(json.dumps(body))


def test_machine_profiles_accept_a_bare_machine_or_full_presets() -> None:
    settings = SlicerSettings.model_validate(
        {
            "process_profile": "0.20mm Standard @BBL X1C",
            "filament_profiles": {"pla": "Bambu PLA Basic @BBL X1C"},
            "machine_profiles": {
                "bambu_p1s_01": "Bambu Lab P1S 0.4 nozzle",
                "bambu_a1_01": {
                    "machine": "Bambu Lab A1 0.4 nozzle",
                    "process": "0.20mm Standard @BBL A1",
                    "filaments": {"pla": "Bambu PLA Basic @BBL A1"},
                },
            },
        }
    )
    assert settings.machine_profiles["bambu_p1s_01"].machine == "Bambu Lab P1S 0.4 nozzle"
    assert settings.process_for("bambu_p1s_01") == "0.20mm Standard @BBL X1C"
    assert settings.filaments_for("bambu_p1s_01") == {"PLA": "Bambu PLA Basic @BBL X1C"}
    assert settings.process_for("bambu_a1_01") == "0.20mm Standard @BBL A1"
    assert settings.filaments_for("bambu_a1_01") == {"PLA": "Bambu PLA Basic @BBL A1"}


def test_a_printers_own_presets_are_used(slicer_settings: SlicerSettings) -> None:
    root = slicer_settings.profiles_dir
    assert root is not None
    _preset(root, "machine", "Bambu Lab A1 0.4 nozzle")
    _preset(root, "process", "0.20mm Standard @BBL A1", ["Bambu Lab A1 0.4 nozzle"])
    _preset(root, "filament", "Bambu PLA Basic @BBL A1", ["Bambu Lab A1 0.4 nozzle"])
    settings = slicer_settings.model_copy(
        update={
            "machine_profiles": SlicerSettings.model_validate(
                {
                    "machine_profiles": {
                        "bambu_test_01": "Bambu Lab Test 0.4 nozzle",
                        "bambu_a1_01": {
                            "machine": "Bambu Lab A1 0.4 nozzle",
                            "process": "0.20mm Standard @BBL A1",
                            "filaments": {"PLA": "Bambu PLA Basic @BBL A1"},
                        },
                    }
                }
            ).machine_profiles
        }
    )
    slicer = BambuStudioSlicer(settings)

    machine, process, filament = slicer.resolve_presets(_request(printer_id="bambu_a1_01"))

    assert (machine.stem, process.stem, filament.stem) == (
        "Bambu Lab A1 0.4 nozzle",
        "0.20mm Standard @BBL A1",
        "Bambu PLA Basic @BBL A1",
    )
    # The printer without its own presets still slices with the defaults.
    _, process, filament = slicer.resolve_presets(_request())
    assert process.stem == "0.20mm Standard @BBL X1C"
    assert filament.stem == "Bambu PLA Basic @BBL Test"


def test_a_printers_filaments_replace_the_defaults(slicer_settings: SlicerSettings) -> None:
    root = slicer_settings.profiles_dir
    assert root is not None
    _preset(root, "filament", "Bambu PETG Basic @BBL Test")
    settings = slicer_settings.model_copy(
        update={
            "filament_profiles": {
                "PLA": "Bambu PLA Basic @BBL Test",
                "PETG": "Bambu PETG Basic @BBL Test",
            },
            "machine_profiles": SlicerSettings.model_validate(
                {
                    "machine_profiles": {
                        "bambu_test_01": {
                            "machine": "Bambu Lab Test 0.4 nozzle",
                            "filaments": {"PLA": "Bambu PLA Basic @BBL Test"},
                        }
                    }
                }
            ).machine_profiles,
        }
    )
    slicer = BambuStudioSlicer(settings)

    assert slicer.materials("bambu_test_01") == ["PLA"]
    with pytest.raises(SlicerError, match="materials that can be sliced for it here: PLA$"):
        slicer.resolve_presets(_request(material="PETG"))


@pytest.mark.parametrize("kind", ["process", "filament"])
def test_inherited_machine_restrictions_are_checked(
    slicer_settings: SlicerSettings, kind: str,
) -> None:
    root = slicer_settings.profiles_dir
    assert root is not None
    parent = {"process": "fdm_process_test_common", "filament": "fdm_filament_test_pla"}[kind]
    _preset(root, kind, parent, ["Bambu Lab X1 Carbon 0.4 nozzle"])
    with pytest.raises(SlicerUnavailable, match=f"{kind} preset .* not made for"):
        BambuStudioSlicer(slicer_settings).resolve_presets(_request())


@pytest.mark.parametrize("kind", ["process", "filament"])
def test_a_preset_made_for_another_machine_is_refused(
    slicer_settings: SlicerSettings, kind: str
) -> None:
    root = slicer_settings.profiles_dir
    assert root is not None
    name = {
        "process": "0.20mm Standard @BBL X1C",
        "filament": "Bambu PLA Basic @BBL Test",
    }[kind]
    _preset(root, kind, name, ["Bambu Lab X1 Carbon 0.4 nozzle"])
    slicer = BambuStudioSlicer(slicer_settings)

    with pytest.raises(SlicerUnavailable, match=f"{kind} preset .* not made for"):
        slicer.resolve_presets(_request())

    # Listing the machine makes the same preset acceptable.
    _preset(root, kind, name, ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab Test 0.4 nozzle"])
    slicer.resolve_presets(_request())


def test_discard_stays_inside_the_work_dir(slicer_settings: SlicerSettings, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        BambuStudioSlicer(slicer_settings).discard(tmp_path)


# -- through the HTTP intake -----------------------------------------------------


@pytest.fixture
def slicing_client(
    settings: Settings, slicer_settings: SlicerSettings, backend: FakeBackend
) -> Iterator[TestClient]:
    payload = settings.model_dump(mode="json")
    payload["printers"][0]["profile"]["plate"] = "textured_plate"
    built = Settings.model_validate(payload)
    built.slicer = slicer_settings
    app = create_app(settings=built, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        yield client


def test_an_stl_upload_is_sliced_validated_and_queued(
    slicing_client: TestClient, slicer_settings: SlicerSettings, backend: FakeBackend
) -> None:
    response = slicing_client.post(
        "/submissions",
        files={"file": ("Cube 20mm.stl", b"solid cube\nendsolid\n", "model/stl")},
        data={"target_machine": "bambu_test_01", "requested_by": "u", "material": "pla"},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["state"] == "queued"
    assert job["artifact_kind"] == "3mf"
    assert job["original_filename"] == "Cube 20mm.3mf"
    assert job["provenance"]["source_filename"] == "Cube 20mm.stl"
    assert job["provenance"]["filament_preset"] == "Bambu PLA Basic @BBL Test"
    # Scratch space is cleaned up; the job owns the only copy.
    assert list(slicer_settings.work_dir.iterdir()) == []
    assert backend.uploads == []


def test_an_stl_can_be_placed_front_left(
    slicing_client: TestClient, slicer_settings: SlicerSettings
) -> None:
    response = slicing_client.post(
        "/submissions",
        files={"file": ("cube.stl", _cube_stl((40.0, 40.0, 0.0), (20.0, 20.0, 20.0)), "model/stl")},
        data={
            "target_machine": "bambu_test_01", "requested_by": "u",
            "material": "PLA", "placement": "front_left",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["provenance"]["placement"] == "front_left"
    assert list(slicer_settings.work_dir.iterdir()) == []


def test_placement_is_refused_for_an_already_sliced_file(
    slicing_client: TestClient, tmp_path: Path
) -> None:
    artifact = write_3mf(tmp_path / "sliced.3mf").read_bytes()
    response = slicing_client.post(
        "/submissions",
        files={"file": ("sliced.3mf", artifact, "model/3mf")},
        data={"target_machine": "bambu_test_01", "requested_by": "u", "placement": "front_left"},
    )
    assert response.status_code == 422
    assert "only to an .stl" in response.json()["detail"]


def test_an_unknown_placement_is_refused(slicing_client: TestClient) -> None:
    response = slicing_client.post(
        "/submissions",
        files={"file": ("cube.stl", _cube_stl((0.0, 0.0, 0.0), (5.0, 5.0, 5.0)), "model/stl")},
        data={
            "target_machine": "bambu_test_01", "requested_by": "u",
            "material": "PLA", "placement": "back_right",
        },
    )
    assert response.status_code == 422


def test_the_park_position_reaches_provenance_and_pickup_targets(
    settings: Settings, slicer_settings: SlicerSettings, backend: FakeBackend
) -> None:
    payload = settings.model_dump(mode="json")
    payload["printers"][0]["profile"]["plate"] = "textured_plate"
    built = Settings.model_validate(payload)
    built.slicer = _with_park(slicer_settings, 250)
    app = create_app(settings=built, backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        response = client.post(
            "/submissions",
            files={"file": ("cube.stl", b"solid cube\nendsolid\n", "model/stl")},
            data={"target_machine": "bambu_test_01", "requested_by": "u", "material": "PLA"},
        )
        assert response.status_code == 201, response.text
        job = response.json()
        assert job["provenance"]["end_bed_y_mm"] == 250
        pickup = client.get(f"/submissions/{job['submission_id']}/pickup-targets")
        assert pickup.status_code == 200, pickup.text
        assert pickup.json()["bed_park_y_mm"] == 250
    assert backend.uploads == []


def test_an_stl_needs_a_material(slicing_client: TestClient) -> None:
    response = slicing_client.post(
        "/submissions",
        files={"file": ("cube.stl", b"solid", "model/stl")},
        data={"target_machine": "bambu_test_01", "requested_by": "u"},
    )
    assert response.status_code == 422
    assert "PLA" in response.json()["detail"]


@pytest.mark.parametrize("agent_mode", [False, True])
def test_a1_agent_stl_intake_uses_a1_presets_without_printer_commands(
    settings: Settings, slicer_settings: SlicerSettings, backend: FakeBackend,
    tmp_path: Path, monkeypatch, agent_mode: bool,
) -> None:
    monkeypatch.setenv("BAMBU_EDGE_SHARED_SECRET", "test-edge-secret")
    payload = settings.model_dump(mode="json")
    printer = payload["printers"][0]
    printer.update(id="bambu_a1_01", model="A1", agent_mode_eligible=True)
    printer["profile"].update(plate="textured_plate", enclosure="open")
    payload["dispatch"] = {"enabled": True, "require_verified_identity": True}
    slicer_payload = slicer_settings.model_dump(mode="json")
    machine = "Bambu Lab A1 0.4 nozzle"
    process = "0.20mm Standard @BBL A1"
    filament = "Generic PLA @BBL A1"
    slicer_payload["machine_profiles"]["bambu_a1_01"] = {
        "machine": machine, "process": process, "filaments": {"PLA": filament},
    }
    payload["slicer"] = slicer_payload
    root = slicer_settings.profiles_dir
    _preset(root, "machine", machine)
    _preset(root, "process", process, [machine])
    _preset(root, "filament", filament, [machine])
    write_3mf(
        tmp_path / "template.3mf", plate_gcode=SAMPLE_GCODE.replace("X1 Carbon", "A1"),
        project_settings={"printer_model": "Bambu Lab A1"},
    )
    app = create_app(settings=Settings.model_validate(payload), backend_factory=lambda _d, _c: backend)
    with TestClient(app) as client:
        if agent_mode:
            response = client.post(
                "/printers/bambu_a1_01/agent-mode",
                headers={"X-Edge-Auth": "test-edge-secret", "X-Auth-User": "admin", "X-Auth-Role": "admin"},
                json={"enabled": True, "reason": "Enable fake-printer slicing test"},
            )
            assert response.status_code == 200
        response = client.post(
            "/submissions",
            headers={"X-Edge-Auth": "test-edge-secret", "X-Auth-User": "test-agent", "X-Auth-Role": "agent", "X-Agent-Id": "test-agent"},
            files={"file": ("coupon.stl", b"solid coupon\nendsolid\n", "model/stl")},
            data={"target_machine": "bambu_a1_01", "material": "PLA"},
        )
        if agent_mode:
            assert response.status_code == 201, response.text
            job = response.json()
            assert job["state"] == "queued"
            assert job["artifact_kind"] == "3mf"
            assert job["provenance"]["machine_preset"] == machine
            assert job["provenance"]["process_preset"] == process
            assert job["provenance"]["filament_preset"] == filament
        else:
            assert response.status_code == 403, response.text
    assert backend.uploads == []
    assert backend.start_commands == []
    assert backend.snapshots == 0


def test_stl_is_refused_where_slicing_is_off(client: TestClient) -> None:
    response = client.post(
        "/submissions",
        files={"file": ("cube.stl", b"solid", "model/stl")},
        data={"target_machine": "bambu_test_01", "requested_by": "u", "material": "PLA"},
    )
    assert response.status_code == 400
