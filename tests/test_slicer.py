"""Slicing an uploaded mesh, with a stand-in for the Bambu Studio CLI.

The stand-in is a small script that behaves like the real CLI's file contract
(read settings, write ``<outputdir>/<name>.3mf`` and ``result.json``), so these
tests need no Bambu Studio install and never run a real slicer.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bambu_server.config import Settings, SlicerSettings
from bambu_server.main import create_app
from bambu_server.slicer import BambuStudioSlicer, SliceRequest, SlicerError, SlicerUnavailable

from .conftest import FakeBackend, write_3mf

FAKE_CLI = """#!{python}
import json, shutil, sys
from pathlib import Path
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1]
out = Path(value("--outputdir"))
settings = value("--load-settings").split(";")
(out / "seen_settings.json").write_text(json.dumps(settings))
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


def _profiles(root: Path) -> Path:
    for kind, name in (
        ("machine", "Bambu Lab Test 0.4 nozzle"),
        ("process", "0.20mm Standard @BBL X1C"),
        ("filament", "Bambu PLA Basic @BBL Test"),
    ):
        (root / kind).mkdir(parents=True, exist_ok=True)
        body = {"name": name}
        if kind == "machine":
            body["nozzle_type"] = ["stainless_steel"]
        (root / kind / f"{name}.json").write_text(json.dumps(body))
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


async def test_a_slicer_failure_is_reported(slicer_settings: SlicerSettings, tmp_path: Path) -> None:
    mesh = tmp_path / "broken.stl"
    mesh.write_text("broken mesh")
    with pytest.raises(SlicerError, match="Nothing to be sliced"):
        await BambuStudioSlicer(slicer_settings).slice(
            mesh, _request(), job_dir=slicer_settings.work_dir / "x"
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


def test_an_stl_needs_a_material(slicing_client: TestClient) -> None:
    response = slicing_client.post(
        "/submissions",
        files={"file": ("cube.stl", b"solid", "model/stl")},
        data={"target_machine": "bambu_test_01", "requested_by": "u"},
    )
    assert response.status_code == 422
    assert "PLA" in response.json()["detail"]


def test_stl_is_refused_where_slicing_is_off(client: TestClient) -> None:
    response = client.post(
        "/submissions",
        files={"file": ("cube.stl", b"solid", "model/stl")},
        data={"target_machine": "bambu_test_01", "requested_by": "u", "material": "PLA"},
    )
    assert response.status_code == 400
