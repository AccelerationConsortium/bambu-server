"""Slice an uploaded mesh into a printable ``.3mf`` with the Bambu Studio CLI.

The slicer is a subprocess, never a library: Bambu Studio is a desktop
application whose command line happens to slice headlessly. Everything it needs
is written into a per-job scratch directory under the configured work dir --
the mesh, an override settings file, Bambu Studio's own ``--datadir`` -- and
the one thing read back is the ``.3mf`` it exports plus its ``result.json``.

Every preset is written out **flattened**: its whole ``inherits`` chain folded
into one self-contained file. The CLI does not resolve ``inherits`` for a file
outside its bundled profiles, so a copy carrying only the child's own keys
silently loses what the parents define -- the bed size, the machine start gcode,
the filament temperatures. Until 2026-10-21 that is exactly what happened: an A1
slice came out with a 200 x 200 bed, a two-line ``G28`` start instead of the
A1's (no wipe, no leveling, no purge) and generic 200 C PLA.

Two facts the gateway knows and the bundled presets do not are forced through
the override file:

* the **nozzle type** fitted to the target machine (the stock P1S preset says
  stainless steel; the lab's printer has hardened steel, and the validator
  rightly rejects the mismatch), and
* the **build plate** declared for the machine, as ``curr_bed_type``, so the
  bed temperatures and the plate named in the gcode belong to the surface the
  printer actually has.

Placement: by default the CLI auto-arranges the part onto the plate centre.
``front_left`` instead puts the part's footprint a margin in from the front-left
corner of the printable area (the origin of the bed frame). The CLI has no
translate option and drops a bare STL off the plate when arrange is disabled,
so the mesh is wrapped in a minimal 3MF whose build-item transform carries the
offset, and the slice runs with ``--arrange 0``.

Slicing touches no printer. It is pure computation on the gateway host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import BedPlate, SlicerSettings
from .plates import PRINTER_TO_SLICER

logger = logging.getLogger(__name__)

_PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9 ._@+()-]+$")
#: Bundled chains are 3-5 deep; anything near this is a loop or a bad file.
_MAX_INHERITS_DEPTH = 16
#: A preset's identity, not settings: never copied from an included template.
_PRESET_META_KEYS = {"name", "inherits", "include", "instantiation", "setting_id", "from", "type"}
#: Result codes Bambu Studio's CLI writes to ``result.json``; 0 is success.
_SUCCESS_RETURN_CODE = 0


#: Where an uploaded mesh goes on the plate. ``auto`` is the slicer's own
#: arrangement (centred); ``front_left`` is a margin in from the bed origin.
Placement = Literal["auto", "front_left"]
PLACEMENTS: tuple[Placement, ...] = ("auto", "front_left")


class SlicerError(RuntimeError):
    """The mesh could not be sliced. The message is safe to show a submitter."""


class SlicerUnavailable(SlicerError):
    """Slicing is not configured for this deployment or this machine."""


@dataclass(frozen=True)
class SliceRequest:
    printer_id: str
    material: str
    nozzle_type: str | None
    plate: BedPlate | None
    placement: Placement = "auto"


@dataclass(frozen=True)
class SliceResult:
    artifact: Path
    slicer_version: str | None
    estimated_minutes: float | None
    warnings: tuple[str, ...]
    #: Where the mesh's footprint was put, front-left corner first, in bed
    #: millimetres; None when the slicer arranged it.
    placed_min_xy_mm: tuple[float, float] | None = None
    #: The Y axis position the end gcode parks the bed at, when the gateway
    #: set it; None when the stock end gcode was left alone.
    end_bed_y_mm: float | None = None


class BambuStudioSlicer:
    def __init__(self, settings: SlicerSettings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @property
    def settings(self) -> SlicerSettings:
        return self._settings

    def accepts(self, printer_id: str) -> bool:
        """Whether an ``.stl`` can be sliced for this printer here."""

        return self._settings.enabled and printer_id in self._settings.machine_profiles

    def materials(self, printer_id: str) -> list[str]:
        """The materials an ``.stl`` can be sliced in for this printer."""

        return sorted(self._settings.filaments_for(printer_id))

    def resolve_presets(self, request: SliceRequest) -> tuple[Path, Path, Path]:
        """Locate the machine, process and filament preset files, or refuse.

        The process and filament presets must be made for the machine preset:
        when a bundled preset lists ``compatible_printers``, the machine must be
        among them. Bambu Studio's CLI does not enforce this itself, and a
        process tuned for one printer's motion slices quietly wrong for another.
        """

        settings = self._settings
        if not settings.enabled:
            raise SlicerUnavailable("slicing is not enabled on this gateway")
        if settings.profiles_dir is None:
            raise SlicerUnavailable("the slicer's profiles directory is not configured")
        presets = settings.presets_for(request.printer_id)
        if presets is None:
            raise SlicerUnavailable(
                f"{request.printer_id} has no slicer machine preset; upload a sliced .3mf instead"
            )
        machine_name = presets.machine
        process_name = settings.process_for(request.printer_id)
        filament_name = settings.filaments_for(request.printer_id).get(
            request.material.strip().upper()
        )
        if filament_name is None:
            offered = ", ".join(self.materials(request.printer_id)) or "none"
            raise SlicerError(
                f"no filament preset for {request.material!r} on {request.printer_id}; "
                f"materials that can be sliced for it here: {offered}"
            )
        for name in (machine_name, process_name, filament_name):
            if not _PRESET_NAME_RE.match(name):
                raise SlicerUnavailable(f"preset name {name!r} contains characters not allowed")
        root = settings.profiles_dir
        machine = root / "machine" / f"{machine_name}.json"
        process = root / "process" / f"{process_name}.json"
        filament = root / "filament" / f"{filament_name}.json"
        for path in (machine, process, filament):
            if not path.is_file():
                raise SlicerUnavailable(f"slicer preset not found: {path.name}")
        for kind, path in (("process", process), ("filament", filament)):
            compatible = _compatible_printers(path)
            if compatible and machine_name not in compatible:
                raise SlicerUnavailable(
                    f"{kind} preset {path.stem!r} is not made for {machine_name!r}; "
                    f"configure a {kind} preset for {request.printer_id}"
                )
        return machine, process, filament

    async def slice(
        self, mesh: Path, request: SliceRequest, *, job_dir: Path
    ) -> SliceResult:
        """Slice ``mesh`` into ``job_dir / 'plate.3mf'``.

        ``job_dir`` must be a fresh directory the caller owns; everything the
        slicer writes lands inside it, and the caller decides when to remove it.
        """

        machine, process, filament = self.resolve_presets(request)
        if request.plate is None:
            raise SlicerError(
                f"{request.printer_id} has no declared build plate, so the slice cannot "
                "target the right surface"
            )
        job_dir.mkdir(parents=True, exist_ok=True)
        root = machine.parent.parent
        machine_body = _flatten_preset(root, "machine", machine.stem)
        process_body = _flatten_preset(root, "process", process.stem)
        filament_body = _flatten_preset(root, "filament", filament.stem)
        if request.nozzle_type:
            # The stock preset carries the value as a one-element list.
            machine_body["nozzle_type"] = [request.nozzle_type]
        # `curr_bed_type` is a project-level setting; the CLI takes it from any
        # loaded settings file and the exported plate gcode carries it.
        machine_body["curr_bed_type"] = PRINTER_TO_SLICER[request.plate]
        presets = self._settings.presets_for(request.printer_id)
        end_bed_y = presets.end_bed_y_mm if presets is not None else None
        if end_bed_y is not None:
            machine_body["machine_end_gcode"] = _with_end_bed_y(
                machine_body.get("machine_end_gcode"),
                end_bed_y,
                printable_area=_printable_area(machine_body),
            )
        override = job_dir / "machine.json"
        override.write_text(json.dumps(machine_body, indent=2), encoding="utf-8")
        process = job_dir / "process.json"
        process.write_text(json.dumps(process_body, indent=2), encoding="utf-8")
        filament = job_dir / "filament.json"
        filament.write_text(json.dumps(filament_body, indent=2), encoding="utf-8")
        datadir = job_dir / "datadir"
        datadir.mkdir(exist_ok=True)
        output = job_dir / "plate.3mf"

        model_input = mesh
        arrange = "1"
        placed: tuple[float, float] | None = None
        if request.placement == "front_left":
            model_input = job_dir / "placed.3mf"
            placed = await asyncio.to_thread(
                _place_front_left,
                mesh,
                model_input,
                printable_area=_printable_area(machine_body),
                margin_mm=self._settings.placement_margin_mm,
            )
            arrange = "0"

        argv = [
            str(self._settings.executable),
            "--debug", "1",
            "--datadir", str(datadir),
            "--load-settings", f"{override};{process}",
            "--load-filaments", str(filament),
            "--arrange", arrange,
            "--orient", "0",
            "--slice", "0",
            "--export-3mf", output.name,
            "--outputdir", str(job_dir),
            str(model_input),
        ]
        logger.info(
            "Slicing %s for %s (%s, placement %s)",
            mesh.name, request.printer_id, request.material, request.placement,
        )
        try:
            process_handle = await asyncio.create_subprocess_exec(
                *argv,
                cwd=job_dir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={"LC_ALL": "C", "HOME": str(job_dir), "PATH": "/usr/bin:/bin"},
            )
        except FileNotFoundError as exc:
            raise SlicerUnavailable("the slicer executable is not installed") from exc
        try:
            _, stderr = await asyncio.wait_for(
                process_handle.communicate(), timeout=self._settings.timeout_s
            )
        except asyncio.TimeoutError as exc:
            process_handle.kill()
            await process_handle.wait()
            raise SlicerError(
                f"slicing did not finish within {int(self._settings.timeout_s)} s"
            ) from exc

        result_path = job_dir / "result.json"
        result: dict[str, object] = {}
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result = {}
        return_code = result.get("return_code", process_handle.returncode)
        if return_code != _SUCCESS_RETURN_CODE or not output.is_file():
            reason = str(result.get("error_string") or "").strip()
            if not reason:
                tail = (stderr or b"")[-400:].decode("utf-8", errors="replace").strip()
                reason = tail.splitlines()[-1] if tail else "no reason reported"
            raise SlicerError(f"slicing failed: {reason}")

        warnings: list[str] = []
        estimated: float | None = None
        for plate in result.get("sliced_plates") or []:
            if not isinstance(plate, dict):
                continue
            warning = str(plate.get("warning_message") or "").strip()
            if warning:
                warnings.append(warning)
            prediction = plate.get("total_predication")
            if isinstance(prediction, (int, float)) and estimated is None:
                estimated = float(prediction) / 60.0
        return SliceResult(
            artifact=output,
            slicer_version=_slicer_version(stderr),
            estimated_minutes=estimated,
            warnings=tuple(warnings),
            placed_min_xy_mm=placed,
            end_bed_y_mm=end_bed_y,
        )

    def discard(self, job_dir: Path) -> None:
        """Remove a job's scratch directory. Only ever inside the work dir."""

        root = self._settings.work_dir.resolve()
        target = job_dir.resolve()
        if root not in target.parents:
            raise ValueError("refusing to remove a directory outside the slicer work dir")
        shutil.rmtree(target, ignore_errors=True)


def _flatten_preset(
    root: Path, kind: str, name: str, _depth: int = 0
) -> dict[str, object]:
    """``root/kind/name.json`` with its ``inherits`` chain folded in, child winning.

    Each level's ``include`` list (newer presets keep their gcode in separate
    "template" presets, e.g. the A1's machine start gcode) is merged just
    before that level's own keys, so a preset's own value still wins.
    """

    chain: list[dict[str, object]] = []
    visited: set[str] = set()
    current = name
    while current:
        if current in visited or len(chain) >= _MAX_INHERITS_DEPTH:
            raise SlicerUnavailable(f"{kind} preset {name!r} has an inherits loop")
        visited.add(current)
        if not _PRESET_NAME_RE.match(current):
            raise SlicerUnavailable(f"preset name {current!r} contains characters not allowed")
        path = root / kind / f"{current}.json"
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SlicerUnavailable(f"slicer preset not found: {kind}/{path.name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SlicerUnavailable(f"{kind} preset {path.name} could not be read") from exc
        if not isinstance(body, dict):
            raise SlicerUnavailable(f"{kind} preset {path.name} is not a settings object")
        body.setdefault("name", current)
        chain.append(body)
        parent = body.get("inherits")
        current = parent.strip() if isinstance(parent, str) else ""
    if _depth > _MAX_INHERITS_DEPTH:
        raise SlicerUnavailable(f"{kind} preset {name!r} includes too deeply")
    merged: dict[str, object] = {}
    for body in reversed(chain):
        includes = body.get("include") or []
        if not isinstance(includes, list):
            raise SlicerUnavailable(f"{kind} preset {body.get('name')!r} has a malformed include")
        for included in includes:
            fragment = _flatten_preset(root, kind, str(included).strip(), _depth + 1)
            merged.update({k: v for k, v in fragment.items() if k not in _PRESET_META_KEYS})
        merged.update(body)
    merged.pop("inherits", None)
    merged.pop("include", None)
    return merged


def _compatible_printers(preset: Path) -> list[str]:
    """The machine presets a bundled preset declares itself made for.

    Empty means the preset makes no claim (a user preset, or one that relies on
    ``compatible_printers_condition``), and nothing is enforced.
    """

    body = _flatten_preset(preset.parent.parent, preset.parent.name, preset.stem)
    listed = body.get("compatible_printers")
    if not isinstance(listed, list):
        return []
    return [name for name in listed if isinstance(name, str)]


#: The stock end gcode's last park move, e.g. the A1's ``G1 X-48 Y180 F3600``:
#: toolhead to the left parking spot, bed to a Y position, just before the
#: motors are released. Only the Y word is ever rewritten.
_END_PARK_MOVE_RE = re.compile(
    r"^(?P<head>G1 X-?\d+(?:\.\d+)? )Y\d+(?:\.\d+)?(?P<tail> F\d+[^\n]*)$", re.MULTILINE
)


def _with_end_bed_y(
    end_gcode: object, y_mm: float, *, printable_area: tuple[float, float, float, float]
) -> str:
    """The machine end gcode with its final bed park move set to ``y_mm``.

    The last absolute ``G1 X.. Y.. F..`` after the stock ``G90`` is the park
    move. A preset whose end gcode does not have one is refused rather than
    guessed at, and so is a position outside the printable area.
    """

    _, area_min_y, _, area_max_y = printable_area
    if not area_min_y <= y_mm <= area_max_y:
        raise SlicerUnavailable(
            f"end_bed_y_mm {y_mm:g} is outside the bed's Y travel "
            f"({area_min_y:g}-{area_max_y:g} mm)"
        )
    if not isinstance(end_gcode, str):
        raise SlicerUnavailable("the machine preset has no end gcode to set the bed position in")
    matches = list(_END_PARK_MOVE_RE.finditer(end_gcode))
    if not matches:
        raise SlicerUnavailable(
            "the machine end gcode has no final park move, so end_bed_y_mm cannot be applied"
        )
    last = matches[-1]
    if "G90" not in end_gcode[: last.start()]:
        raise SlicerUnavailable(
            "the machine end gcode's park move is not in absolute positioning"
        )
    replacement = (
        f"{last.group('head')}Y{y_mm:g}{last.group('tail')} "
        "; bed parked for pickup (gateway end_bed_y_mm)"
    )
    return end_gcode[: last.start()] + replacement + end_gcode[last.end():]


_STL_TRIANGLE = struct.Struct("<12x9f2x")
_ASCII_VERTEX_RE = re.compile(rb"^\s*vertex\s+(\S+)\s+(\S+)\s+(\S+)", re.MULTILINE)
_AREA_POINT_RE = re.compile(r"^\s*(-?[0-9.]+)\s*x\s*(-?[0-9.]+)\s*$")

Vertex = tuple[float, float, float]


def _read_stl(path: Path) -> tuple[list[Vertex], list[tuple[int, int, int]]]:
    """An STL's shared vertices and its triangles as vertex indices.

    Binary STL is recognised by its exact length (80-byte header, a triangle
    count, 50 bytes per triangle); anything else starting with ``solid`` is
    read as ASCII. Vertices are shared by exact value so the 3MF stays one
    connected mesh, as the STL describes it.
    """

    data = path.read_bytes()
    coordinates: list[float]
    if len(data) >= 84:
        (count,) = struct.unpack_from("<I", data, 80)
    else:
        count = -1
    if count >= 0 and len(data) == 84 + 50 * count:
        coordinates = [
            value for triangle in _STL_TRIANGLE.iter_unpack(data[84:]) for value in triangle
        ]
    elif data.lstrip().startswith(b"solid"):
        try:
            coordinates = [
                float(value) for match in _ASCII_VERTEX_RE.finditer(data) for value in match.groups()
            ]
        except ValueError as exc:
            raise SlicerError("the uploaded mesh is not a readable STL") from exc
        if len(coordinates) % 9:
            raise SlicerError("the uploaded mesh is not a readable STL")
    else:
        raise SlicerError("the uploaded mesh is not a readable STL")
    if not coordinates:
        raise SlicerError("the uploaded mesh has no triangles")
    if any(value != value or value in (float("inf"), float("-inf")) for value in coordinates):
        raise SlicerError("the uploaded mesh has non-finite coordinates")

    index: dict[Vertex, int] = {}
    vertices: list[Vertex] = []
    triangles: list[tuple[int, int, int]] = []
    for start in range(0, len(coordinates), 9):
        corners = []
        for offset in (0, 3, 6):
            vertex = (
                coordinates[start + offset],
                coordinates[start + offset + 1],
                coordinates[start + offset + 2],
            )
            found = index.get(vertex)
            if found is None:
                found = index[vertex] = len(vertices)
                vertices.append(vertex)
            corners.append(found)
        if len(set(corners)) == 3:  # a degenerate triangle has no area to print
            triangles.append((corners[0], corners[1], corners[2]))
    if not triangles:
        raise SlicerError("the uploaded mesh has no triangles")
    return vertices, triangles


def _printable_area(machine_body: dict[str, object]) -> tuple[float, float, float, float]:
    """The machine's printable rectangle as ``(min_x, min_y, max_x, max_y)``."""

    points: list[tuple[float, float]] = []
    raw = machine_body.get("printable_area")
    for entry in raw if isinstance(raw, list) else []:
        match = _AREA_POINT_RE.match(str(entry))
        if match:
            points.append((float(match.group(1)), float(match.group(2))))
    if len(points) < 3:
        raise SlicerUnavailable("the machine preset does not declare its printable area")
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def _place_front_left(
    mesh: Path,
    output: Path,
    *,
    printable_area: tuple[float, float, float, float],
    margin_mm: float,
) -> tuple[float, float]:
    """Write ``mesh`` as a one-object 3MF placed at the front-left of the bed.

    The part keeps its orientation; it is only translated so its footprint's
    front-left corner sits ``margin_mm`` in from the printable area's and its
    lowest point rests on the plate. The margin leaves room for a brim or a
    skirt. A part too large to fit with the margin is refused rather than
    hung off the bed. Returns the footprint's new front-left corner.
    """

    vertices, triangles = _read_stl(mesh)
    min_x = min(v[0] for v in vertices)
    min_y = min(v[1] for v in vertices)
    min_z = min(v[2] for v in vertices)
    size_x = max(v[0] for v in vertices) - min_x
    size_y = max(v[1] for v in vertices) - min_y
    area_min_x, area_min_y, area_max_x, area_max_y = printable_area
    target_x = area_min_x + margin_mm
    target_y = area_min_y + margin_mm
    if target_x + size_x > area_max_x or target_y + size_y > area_max_y:
        raise SlicerError(
            f"the part ({size_x:.1f} x {size_y:.1f} mm) does not fit at the front left "
            f"with a {margin_mm:g} mm margin on a "
            f"{area_max_x - area_min_x:g} x {area_max_y - area_min_y:g} mm bed"
        )
    tx, ty, tz = target_x - min_x, target_y - min_y, -min_z

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" '
            'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>",
        )
        with archive.open("3D/3dmodel.model", "w") as model:
            model.write(
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                b'<model unit="millimeter" xml:lang="en-US" '
                b'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                b'<resources><object id="1" type="model"><mesh><vertices>'
            )
            model.write(
                "".join(
                    f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>' for x, y, z in vertices
                ).encode("ascii")
            )
            model.write(b"</vertices><triangles>")
            model.write(
                "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in triangles).encode(
                    "ascii"
                )
            )
            model.write(
                b"</triangles></mesh></object></resources><build>"
                + f'<item objectid="1" transform="1 0 0 0 1 0 0 0 1 '
                f'{tx:.6f} {ty:.6f} {tz:.6f}"/>'.encode("ascii")
                + b"</build></model>"
            )
    return round(target_x, 3), round(target_y, 3)


_VERSION_RE = re.compile(r"BambuStudio Version (\S+)")


def _slicer_version(stderr: bytes | None) -> str | None:
    if not stderr:
        return None
    match = _VERSION_RE.search(stderr.decode("utf-8", errors="replace"))
    return match.group(1) if match else None
