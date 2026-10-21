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

Slicing touches no printer. It is pure computation on the gateway host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class SliceResult:
    artifact: Path
    slicer_version: str | None
    estimated_minutes: float | None
    warnings: tuple[str, ...]


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

    def materials(self) -> list[str]:
        return sorted(self._settings.filament_profiles)

    def resolve_presets(self, request: SliceRequest) -> tuple[Path, Path, Path]:
        """Locate the machine, process and filament preset files, or refuse."""

        settings = self._settings
        if not settings.enabled:
            raise SlicerUnavailable("slicing is not enabled on this gateway")
        if settings.profiles_dir is None:
            raise SlicerUnavailable("the slicer's profiles directory is not configured")
        machine_name = settings.machine_profiles.get(request.printer_id)
        if machine_name is None:
            raise SlicerUnavailable(
                f"{request.printer_id} has no slicer machine preset; upload a sliced .3mf instead"
            )
        filament_name = settings.filament_profiles.get(request.material.strip().upper())
        if filament_name is None:
            offered = ", ".join(self.materials()) or "none"
            raise SlicerError(
                f"no filament preset for {request.material!r}; materials that can be sliced "
                f"here: {offered}"
            )
        for name in (machine_name, settings.process_profile, filament_name):
            if not _PRESET_NAME_RE.match(name):
                raise SlicerUnavailable(f"preset name {name!r} contains characters not allowed")
        root = settings.profiles_dir
        machine = root / "machine" / f"{machine_name}.json"
        process = root / "process" / f"{settings.process_profile}.json"
        filament = root / "filament" / f"{filament_name}.json"
        for path in (machine, process, filament):
            if not path.is_file():
                raise SlicerUnavailable(f"slicer preset not found: {path.name}")
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
        override = job_dir / "machine.json"
        override.write_text(json.dumps(machine_body, indent=2), encoding="utf-8")
        process = job_dir / "process.json"
        process.write_text(json.dumps(process_body, indent=2), encoding="utf-8")
        filament = job_dir / "filament.json"
        filament.write_text(json.dumps(filament_body, indent=2), encoding="utf-8")
        datadir = job_dir / "datadir"
        datadir.mkdir(exist_ok=True)
        output = job_dir / "plate.3mf"

        argv = [
            str(self._settings.executable),
            "--debug", "1",
            "--datadir", str(datadir),
            "--load-settings", f"{override};{process}",
            "--load-filaments", str(filament),
            "--arrange", "1",
            "--orient", "0",
            "--slice", "0",
            "--export-3mf", output.name,
            "--outputdir", str(job_dir),
            str(mesh),
        ]
        logger.info("Slicing %s for %s (%s)", mesh.name, request.printer_id, request.material)
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


_VERSION_RE = re.compile(r"BambuStudio Version (\S+)")


def _slicer_version(stderr: bytes | None) -> str | None:
    if not stderr:
        return None
    match = _VERSION_RE.search(stderr.decode("utf-8", errors="replace"))
    return match.group(1) if match else None
