"""Read-only inspection of a submitted print artifact.

This module answers "what does this file say it will do?" and nothing else. It
opens no network connection, touches no printer, and never executes anything it
reads. Everything it returns is an observation about the file, so a caller can
compare those observations against a machine profile.

Two artifact shapes are understood:

* ``.gcode`` — a slicer's toolpath. Its ``; key = value`` comment block carries
  the print settings, and its motion commands carry the real plate footprint.
* ``.3mf`` — a zip container. A *sliced* plate file (what a printer can
  actually run) embeds ``Metadata/plate_N.gcode``, which is scanned exactly like
  a bare ``.gcode``; the sidecar ``project_settings.config`` and
  ``slice_info.config`` fill in settings and the slicer's own time estimate.
  An unsliced project file is reported as ``sliced=False`` rather than guessed
  at — no printer can run one.

Everything here is bounded. Submitted files are untrusted input: reads are
capped, XML with a document type declaration is refused outright (that is the
only place an entity expansion can be declared), and a scan that hits its budget
reports ``scan_truncated`` and withholds the measurements it could not complete
rather than reporting a partial answer as a whole one.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import IO, Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from .plates import plate_from_slicer

ArtifactKind = Literal["3mf", "gcode"]

#: Extensions the intake accepts, mapped to the kind the inspector reports.
ARTIFACT_EXTENSIONS: dict[str, ArtifactKind] = {".3mf": "3mf", ".gcode": "gcode"}

# Sidecar config members are small (tens of KB in practice); this is a generous
# ceiling that still refuses a zip bomb dressed up as a settings file.
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_FINDINGS = 20

_PLATE_GCODE_RE = re.compile(r"^Metadata/plate_\d+\.gcode$", re.IGNORECASE)
_COMMENT_SETTING_RE = re.compile(r"^;\s*([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$")
# A slicer header typically states two times on one line: the total estimate
# (wall clock, including heating and non-printing moves) and the model printing
# time. The total is the one a queue estimate wants, so it is matched
# separately and preferred over the fallback rather than by regex ordering.
_TOTAL_TIME_RE = re.compile(
    r"total estimated time\s*[:=]\s*([0-9hmsd. ]+)", re.IGNORECASE
)
_FALLBACK_TIME_RE = re.compile(
    r"(?:estimated printing time[^:=]*|model printing time)\s*[:=]\s*([0-9hmsd. ]+)",
    re.IGNORECASE,
)
_HMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.IGNORECASE)
_AXIS_RE = re.compile(r"([XYZ])(-?\d+(?:\.\d+)?)")
# A gcode word is a letter followed by a number (G1, M104, T0). Lines that do
# not start with one are not counted as commands, so a text file renamed to
# .gcode reports `sliced=False` instead of a toolpath it does not contain.
_COMMAND_WORD_RE = re.compile(r"^[A-Z]-?\d+(?:\.\d+)?$")
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)

# Commands refused outright. Each either rewrites persistent machine state or
# takes the printer out of normal operation; none belongs in a print job.
_FORBIDDEN_COMMANDS: dict[str, str] = {
    "M92": "sets axis steps per unit",
    "M301": "rewrites hotend PID tuning",
    "M302": "allows cold extrusion",
    "M303": "runs a PID autotune cycle",
    "M304": "rewrites bed PID tuning",
    "M500": "writes settings to persistent storage",
    "M502": "resets settings to factory defaults",
    "M997": "starts a firmware update",
    "M999": "resets the controller after a halt",
}

# The one M500 accepted (approved 2026-10-20): Bambu's stock machine start
# gcode saves the bed mesh right after probing it ("G29 ... M400 / M500 ; save
# cali data"). It is allowed only directly after a G29, with nothing but M400
# between, and only inside the start gcode -- confirmed by the slicer's end
# marker appearing afterwards. Any other M500 is refused as before.
_START_GCODE_END_MARKER = "MACHINE_START_GCODE_END"

_NOZZLE_TEMP_COMMANDS = {"M104", "M109"}
_BED_TEMP_COMMANDS = {"M140", "M190"}

_BED_TEMPERATURE_KEYS = (
    "hot_plate_temp",
    "textured_plate_temp",
    "cool_plate_temp",
    "supertack_plate_temp",
    "eng_plate_temp",
    "bed_temperature",
    "first_layer_bed_temperature",
)
_NOZZLE_TEMPERATURE_KEYS = (
    "nozzle_temperature",
    "nozzle_temperature_initial_layer",
    "temperature",
    "first_layer_temperature",
)


class ArtifactError(ValueError):
    """The file cannot be read as the kind its extension claims."""


class GcodeFinding(BaseModel):
    """One suspicious thing seen while scanning toolpath commands."""

    code: str
    detail: str


class FilamentUse(BaseModel):
    """One filament the sliced plate calls for, as ``slice_info.config`` lists it.

    ``filament_id`` is the slicer's 1-based extruder/filament index; it is the
    position a dispatch tray mapping has to fill. The type is what the loaded
    tray must match.
    """

    filament_id: int
    filament_type: str
    color: str | None = None
    used_g: float | None = None


class ArtifactFacts(BaseModel):
    """Everything the inspector could observe about one submitted file.

    A ``None`` means *not observed*, never *zero* or *absent from the model*.
    """

    kind: ArtifactKind
    byte_size: int
    sliced: bool = False
    scan_truncated: bool = False
    filament_types: tuple[str, ...] = ()
    #: Per-filament detail from the 3mf's slice metadata. Empty for a bare
    #: gcode, which names types but not slots.
    filaments: tuple[FilamentUse, ...] = ()
    #: The plate the embedded gcode belongs to (``Metadata/plate_N.gcode``);
    #: what a start command has to name.
    plate_index: int | None = None
    #: The build plate the job was sliced for, in the printer's vocabulary;
    #: ``None`` when the slicer named none or one this service does not know.
    bed_type: str | None = None
    bed_type_label: str | None = None
    nozzle_temperature_c: float | None = None
    bed_temperature_c: float | None = None
    chamber_temperature_c: float | None = None
    nozzle_diameter_mm: float | None = None
    nozzle_type: str | None = None
    layer_height_mm: float | None = None
    #: The highest temperature the toolpath ever *commands*, which can exceed
    #: the configured setpoint if the file was edited after slicing.
    commanded_nozzle_temperature_c: float | None = None
    commanded_bed_temperature_c: float | None = None
    printer_model: str | None = None
    estimated_duration_minutes: float | None = None
    #: X, Y, Z extents in millimetres -- a size, not a position, so it does not
    #: depend on where on the plate the slicer happened to place the model.
    extent_mm: tuple[float, float, float] | None = None
    extent_source: str | None = None
    gcode_findings: tuple[GcodeFinding, ...] = ()
    notes: tuple[str, ...] = Field(default_factory=tuple)


class ObjectBounds(BaseModel):
    """Axis-aligned XY bounds in the printer's bed coordinate system."""

    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float


class ObjectPosition(BaseModel):
    """One independently addressable model or labelled toolpath region.

    ``gcode_bounds`` is an extrusion-only observation, when the slicer emitted
    object labels.  It is intentionally a cross-check rather than a replacement
    for the model transform: purge lines and travel moves are not object shape.
    """

    object_id: str
    name: str | None = None
    center_x_mm: float
    center_y_mm: float
    bounds: ObjectBounds
    rotation_deg: float | None = None
    plate_index: int | None = None
    source: Literal["model_transform", "gcode_label"]
    gcode_bounds: ObjectBounds | None = None


class _Scan:
    """Accumulates observations over one pass of a gcode stream."""

    def __init__(self, *, from_start: bool = False) -> None:
        self.settings: dict[str, str] = {}
        self.total_minutes: float | None = None
        self.fallback_minutes: float | None = None
        self.command_count = 0
        self._findings: list[GcodeFinding] = []
        # Only a scan that begins at the top of the file can be inside the
        # machine start gcode; a tail scan never is.
        self._in_start_gcode = from_start
        self._last_word: str | None = None
        self._unconfirmed_mesh_saves = 0
        self.relative_positioning_seen = False
        self._min = [None, None, None]  # type: list[float | None]
        self._max = [None, None, None]  # type: list[float | None]
        self._absolute = True
        self._position: list[float | None] = [None, None, None]

    @property
    def duration_minutes(self) -> float | None:
        return self.total_minutes if self.total_minutes is not None else self.fallback_minutes

    def extent(self) -> tuple[float, float, float] | None:
        if any(value is None for value in self._min + self._max):
            return None
        low_x, low_y, low_z = self._min
        high_x, high_y, high_z = self._max
        return (
            round(high_x - low_x, 3),  # type: ignore[operator]
            round(high_y - low_y, 3),  # type: ignore[operator]
            round(high_z - low_z, 3),  # type: ignore[operator]
        )

    @property
    def findings(self) -> list[GcodeFinding]:
        findings = list(self._findings)
        if self._unconfirmed_mesh_saves and len(findings) < _MAX_FINDINGS:
            # A G29-then-M500 the scan never saw close the start gcode.
            findings.append(GcodeFinding(
                code="forbidden_command",
                detail=f"M500 {_FORBIDDEN_COMMANDS['M500']}; refused in a submitted job",
            ))
        return findings

    def add_finding(self, code: str, detail: str) -> None:
        if len(self._findings) < _MAX_FINDINGS:
            self._findings.append(GcodeFinding(code=code, detail=detail))

    def observe_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if stripped.startswith(";"):
            self._observe_comment(stripped)
            return
        self._observe_command(stripped)

    def _observe_comment(self, comment: str) -> None:
        if self._in_start_gcode and comment.lstrip(";").strip().upper() == _START_GCODE_END_MARKER:
            self._in_start_gcode = False
            self._unconfirmed_mesh_saves = 0  # they were in the start gcode
        match = _COMMENT_SETTING_RE.match(comment)
        if match:
            key, raw = match.group(1).lower(), match.group(2).strip()
            # First writer wins: a slicer's header block precedes its config
            # block, and re-reading the same key later must not clobber it.
            self.settings.setdefault(key, raw)
        if self.total_minutes is None:
            total = _TOTAL_TIME_RE.search(comment)
            if total:
                self.total_minutes = parse_duration_minutes(total.group(1))
        if self.fallback_minutes is None:
            fallback = _FALLBACK_TIME_RE.search(comment)
            if fallback:
                self.fallback_minutes = parse_duration_minutes(fallback.group(1))

    def _observe_command(self, command_line: str) -> None:
        body = command_line.split(";", 1)[0].strip()
        if not body:
            return
        word = body.split(maxsplit=1)[0].upper()
        if not _COMMAND_WORD_RE.match(word):
            return
        self.command_count += 1
        previous = self._last_word
        if word != "M400":  # a wait between the probe and the save is fine
            self._last_word = word

        if word == "M500" and self._in_start_gcode and previous == "G29":
            self._unconfirmed_mesh_saves += 1
            return

        if word == "G90":
            self._absolute = True
            return
        if word == "G91":
            self._absolute = False
            self.relative_positioning_seen = True
            return

        reason = _FORBIDDEN_COMMANDS.get(word)
        if reason is not None:
            self.add_finding(
                "forbidden_command", f"{word} {reason}; refused in a submitted job"
            )
            return

        if word in _NOZZLE_TEMP_COMMANDS or word in _BED_TEMP_COMMANDS:
            self._observe_temperature_command(word, body)
            return

        if word in {"G0", "G1", "G2", "G3"} and self._absolute:
            self._observe_move(body)

    def _observe_temperature_command(self, word: str, body: str) -> None:
        value = _parse_parameter(body, "S")
        if value is None:
            return
        key = (
            "commanded_nozzle_temperature_c"
            if word in _NOZZLE_TEMP_COMMANDS
            else "commanded_bed_temperature_c"
        )
        previous = self.settings.get(key)
        highest = max(value, float(previous)) if previous is not None else value
        # Overwrite rather than setdefault: the interesting value is the peak
        # the job ever commands, not the first one it happens to command.
        self.settings[key] = str(highest)

    def _observe_move(self, body: str) -> None:
        for axis, raw in _AXIS_RE.findall(body.upper()):
            index = "XYZ".index(axis)
            try:
                value = float(raw)
            except ValueError:
                continue
            self._position[index] = value
            low, high = self._min[index], self._max[index]
            self._min[index] = value if low is None else min(low, value)
            self._max[index] = value if high is None else max(high, value)


def parse_duration_minutes(text: str) -> float | None:
    """Parse ``1h 2m 3s`` / ``45s`` / a bare seconds count into minutes."""

    text = text.strip()
    if not text:
        return None
    matches = _HMS_RE.findall(text)
    if matches:
        scale = {"d": 1440.0, "h": 60.0, "m": 1.0, "s": 1.0 / 60.0}
        return round(
            sum(float(value) * scale[unit.lower()] for value, unit in matches), 2
        )
    try:
        return round(float(text) / 60.0, 2)
    except ValueError:
        return None


def _parse_parameter(body: str, letter: str) -> float | None:
    match = re.search(rf"(?:^|\s){letter}(-?\d+(?:\.\d+)?)", body, re.IGNORECASE)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _first_number(raw: str | None) -> float | None:
    """Take the first value of a scalar or a ``,``/``;``-separated list."""

    if raw is None:
        return None
    head = re.split(r"[,;]", str(raw).strip())[0].strip()
    if not head:
        return None
    try:
        return float(head)
    except ValueError:
        return None


def _split_values(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    seen: list[str] = []
    for part in re.split(r"[,;]", str(raw)):
        token = part.strip().strip('"').upper()
        if token and token not in seen:
            seen.append(token)
    return tuple(seen)


def _scan_stream(stream: IO[bytes], *, budget: int) -> tuple[_Scan, bool]:
    """Scan up to ``budget`` bytes of a gcode stream.

    Returns the scan and whether the stream was cut short. A truncated scan
    still yields settings (slicers write their config block at the top of the
    file) but its motion extents are incomplete, so the caller discards them.
    """

    scan = _Scan(from_start=True)
    consumed = 0
    remainder = b""
    exhausted = False
    while consumed < budget:
        chunk = stream.read(min(1 << 20, budget - consumed))
        if not chunk:
            exhausted = True
            break
        consumed += len(chunk)
        remainder += chunk
        *lines, remainder = remainder.split(b"\n")
        for raw_line in lines:
            scan.observe_line(raw_line.decode("latin-1"))

    # Budget reached: one more byte decides whether anything was left unread.
    truncated = not exhausted and bool(stream.read(1))
    if remainder and not truncated:
        scan.observe_line(remainder.decode("latin-1"))
    return scan, truncated


def _scan_tail(path: Path, *, size: int, budget: int) -> _Scan:
    """Scan the last ``budget`` bytes of a file.

    Some slicers (PrusaSlicer and its derivatives) write their configuration
    block at the *end* of the file, so a head-only scan of a large artifact
    would see no settings at all.
    """

    scan = _Scan()
    with path.open("rb") as handle:
        handle.seek(max(0, size - budget))
        # The first line is probably cut mid-way; dropping it costs nothing.
        handle.readline()
        for raw_line in handle:
            scan.observe_line(raw_line.decode("latin-1"))
    return scan


def _merge_settings(primary: _Scan, secondary: _Scan) -> None:
    for key, value in secondary.settings.items():
        primary.settings.setdefault(key, value)
    if primary.total_minutes is None:
        primary.total_minutes = secondary.total_minutes
    if primary.fallback_minutes is None:
        primary.fallback_minutes = secondary.fallback_minutes
    primary.command_count += secondary.command_count


def _read_member(archive: zipfile.ZipFile, name: str, *, budget: int) -> bytes | None:
    try:
        with archive.open(name) as member:
            data = member.read(budget + 1)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    if len(data) > budget:
        return None
    return data


def _parse_xml(data: bytes) -> ElementTree.Element | None:
    """Parse trusted-shape XML from an untrusted file.

    A document type declaration is the only place an XML entity can be defined,
    and entity expansion is the standard way to turn a small XML file into an
    out-of-memory condition. Bambu's config members never carry one, so refusing
    the whole document is both safe and free of false negatives.
    """

    if _DOCTYPE_RE.search(data[:8192]) or _DOCTYPE_RE.search(data):
        return None
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


_PLATE_INDEX_RE = re.compile(r"plate_(\d+)\.gcode$", re.IGNORECASE)


def _plate_index(member_name: str) -> int | None:
    match = _PLATE_INDEX_RE.search(member_name)
    return int(match.group(1)) if match else None


def _settings_from_project(data: bytes) -> dict[str, str]:
    """Flatten ``Metadata/project_settings.config`` (JSON) into scalar strings."""

    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    settings: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float)):
            settings[str(key).lower()] = str(value)
        elif isinstance(value, list) and value and all(
            isinstance(item, (str, int, float)) for item in value
        ):
            settings[str(key).lower()] = ",".join(str(item) for item in value)
    return settings


class _SliceInfo:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.filament_types: tuple[str, ...] = ()
        self.filaments: tuple[FilamentUse, ...] = ()
        self.duration_minutes: float | None = None


def _slice_info(data: bytes) -> _SliceInfo:
    """Read ``Metadata/slice_info.config`` for plate metadata and filaments."""

    info = _SliceInfo()
    root = _parse_xml(data)
    if root is None:
        return info

    types: list[str] = []
    uses: list[FilamentUse] = []
    for plate in root.iter():
        if _local_name(plate.tag) == "metadata":
            key = (plate.get("key") or "").strip().lower()
            value = (plate.get("value") or "").strip()
            if key and value:
                info.settings.setdefault(key, value)
        elif _local_name(plate.tag) == "filament":
            filament_type = (plate.get("type") or "").strip().upper()
            if not filament_type:
                continue
            if filament_type not in types:
                types.append(filament_type)
            filament_id = _first_number(plate.get("id"))
            if filament_id is not None and filament_id == int(filament_id):
                uses.append(
                    FilamentUse(
                        filament_id=int(filament_id),
                        filament_type=filament_type,
                        color=(plate.get("color") or "").strip() or None,
                        used_g=_first_number(plate.get("used_g")),
                    )
                )
    info.filament_types = tuple(types)
    info.filaments = tuple(sorted(uses, key=lambda use: use.filament_id))
    prediction = info.settings.get("prediction")
    if prediction:
        info.duration_minutes = parse_duration_minutes(prediction)
    return info


def _bed_temperature(settings: dict[str, str]) -> float | None:
    """Pick the bed temperature for the plate the job is actually configured for.

    Bambu stores one temperature per plate type and names the selected plate in
    ``curr_bed_type``; reading the wrong one would compare the machine's limit
    against a temperature this job never uses.
    """

    bed_type = (settings.get("curr_bed_type") or "").lower()
    preferred: str | None = None
    if "textured" in bed_type:
        preferred = "textured_plate_temp"
    elif "eng" in bed_type:
        preferred = "eng_plate_temp"
    elif "supertack" in bed_type:
        preferred = "supertack_plate_temp"
    elif "cool" in bed_type or "pla plate" in bed_type:
        preferred = "cool_plate_temp"
    elif bed_type:
        preferred = "hot_plate_temp"

    for key in ((preferred,) if preferred else ()) + _BED_TEMPERATURE_KEYS:
        value = _first_number(settings.get(key))
        if value is not None:
            return value
    return _first_number(settings.get("commanded_bed_temperature_c"))


def _facts_from_settings(
    *,
    kind: ArtifactKind,
    byte_size: int,
    sliced: bool,
    scan: _Scan,
    truncated: bool,
    extra_filaments: tuple[str, ...],
    extra_duration: float | None,
    extent_source: str | None,
    notes: list[str],
    filament_uses: tuple[FilamentUse, ...] = (),
    plate_index: int | None = None,
) -> ArtifactFacts:
    settings = scan.settings
    filaments = _split_values(settings.get("filament_type")) or extra_filaments
    bed_type = plate_from_slicer(settings.get("curr_bed_type"))
    bed_type_label = (settings.get("curr_bed_type") or "").strip() or None
    if bed_type_label and bed_type is None:
        notes.append(f"the sliced plate type {bed_type_label!r} is not one this service knows")

    extent = None if truncated else scan.extent()
    if truncated and scan.extent() is not None:
        notes.append(
            "plate footprint not reported: the artifact exceeded the scan budget "
            "so its motion commands were only partly read"
        )
    if scan.relative_positioning_seen:
        extent = None
        notes.append(
            "plate footprint not reported: the toolpath uses relative positioning"
        )

    nozzle_temperature = None
    for key in _NOZZLE_TEMPERATURE_KEYS:
        nozzle_temperature = _first_number(settings.get(key))
        if nozzle_temperature is not None:
            break
    if nozzle_temperature is None:
        nozzle_temperature = _first_number(settings.get("commanded_nozzle_temperature_c"))

    duration = scan.duration_minutes if scan.duration_minutes is not None else extra_duration

    return ArtifactFacts(
        kind=kind,
        byte_size=byte_size,
        sliced=sliced,
        scan_truncated=truncated,
        filament_types=filaments,
        filaments=filament_uses,
        plate_index=plate_index,
        bed_type=bed_type,
        bed_type_label=bed_type_label,
        nozzle_temperature_c=nozzle_temperature,
        bed_temperature_c=_bed_temperature(settings),
        chamber_temperature_c=_first_number(settings.get("chamber_temperature")),
        nozzle_diameter_mm=_first_number(settings.get("nozzle_diameter")),
        nozzle_type=(settings.get("nozzle_type") or "").strip().lower() or None,
        layer_height_mm=_first_number(settings.get("layer_height")),
        commanded_nozzle_temperature_c=_first_number(
            settings.get("commanded_nozzle_temperature_c")
        ),
        commanded_bed_temperature_c=_first_number(
            settings.get("commanded_bed_temperature_c")
        ),
        printer_model=(settings.get("printer_model") or "").strip() or None,
        estimated_duration_minutes=duration,
        extent_mm=extent,
        extent_source=extent_source if extent is not None else None,
        gcode_findings=tuple(scan.findings),
        notes=tuple(notes),
    )


def _inspect_gcode(path: Path, *, scan_max_bytes: int) -> ArtifactFacts:
    size = path.stat().st_size
    notes: list[str] = []
    with path.open("rb") as handle:
        scan, truncated = _scan_stream(handle, budget=scan_max_bytes)
    if truncated:
        # A trailing config block is the norm for some slicers, so read the tail
        # before concluding that a large file declares no settings at all.
        _merge_settings(scan, _scan_tail(path, size=size, budget=min(scan_max_bytes, 1 << 22)))
    if scan.command_count == 0:
        notes.append("no gcode commands were found in the scanned region")
    return _facts_from_settings(
        kind="gcode",
        byte_size=size,
        sliced=scan.command_count > 0,
        scan=scan,
        truncated=truncated,
        extra_filaments=(),
        extra_duration=None,
        extent_source="gcode_motion",
        notes=notes,
    )


def _inspect_3mf(path: Path, *, scan_max_bytes: int) -> ArtifactFacts:
    size = path.stat().st_size
    notes: list[str] = []
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:  # pragma: no cover - guarded at intake
        raise ArtifactError("the 3mf container could not be opened") from exc

    with archive:
        names = archive.namelist()
        project = _read_member(archive, "Metadata/project_settings.config", budget=_MAX_CONFIG_BYTES)
        slice_member = _read_member(archive, "Metadata/slice_info.config", budget=_MAX_CONFIG_BYTES)

        plate_names = sorted(name for name in names if _PLATE_GCODE_RE.match(name))
        scan = _Scan()
        truncated = False
        plate_index: int | None = None
        if plate_names:
            plate_index = _plate_index(plate_names[0])
            if len(plate_names) > 1:
                notes.append(
                    f"the container holds {len(plate_names)} plates; "
                    f"{plate_names[0]} was inspected"
                )
            with archive.open(plate_names[0]) as member:
                scan, truncated = _scan_stream(member, budget=scan_max_bytes)
            if truncated:
                notes.append(
                    "the embedded plate gcode exceeded the scan budget; "
                    "only its leading section was read"
                )
        else:
            notes.append(
                "no sliced plate gcode is embedded, so this project file cannot "
                "be run by a printer as submitted"
            )

        info = _SliceInfo()
        if slice_member is not None:
            info = _slice_info(slice_member)
        elif "Metadata/slice_info.config" in names:
            notes.append("slice_info.config could not be read")
        slice_settings = info.settings

        project_settings = _settings_from_project(project) if project is not None else {}

    # Precedence: the embedded gcode is what the printer will actually execute,
    # so its own header wins over the sidecar configs that describe intent.
    for source in (slice_settings, project_settings):
        for key, value in source.items():
            scan.settings.setdefault(key, value)

    return _facts_from_settings(
        kind="3mf",
        byte_size=size,
        sliced=bool(plate_names) and scan.command_count > 0,
        scan=scan,
        truncated=truncated,
        extra_filaments=info.filament_types,
        extra_duration=info.duration_minutes,
        extent_source="embedded_plate_gcode",
        notes=notes,
        filament_uses=info.filaments,
        plate_index=plate_index,
    )


def inspect_artifact(path: Path, *, scan_max_bytes: int) -> ArtifactFacts:
    """Observe a submitted artifact. Never contacts a printer."""

    kind = ARTIFACT_EXTENSIONS.get(path.suffix.lower())
    if kind is None:
        raise ArtifactError(f"unsupported artifact extension {path.suffix!r}")
    if kind == "gcode":
        return _inspect_gcode(path, scan_max_bytes=scan_max_bytes)
    return _inspect_3mf(path, scan_max_bytes=scan_max_bytes)


# -- Per-object placement -------------------------------------------------

_OBJECT_LABEL_RE = re.compile(r"^;\s*(?:OBJECT|MESH|PRINTING_OBJECT)\s*[:=]\s*(.+)$", re.I)
_EXCLUDE_OBJECT_RE = re.compile(
    r"^EXCLUDE_OBJECT_DEFINE\s+NAME=([^\s]+).*?CENTER=([-.\d]+),([-.\d]+)", re.I
)
_E_RE = re.compile(r"(?:^|\s)E(-?\d+(?:\.\d+)?)", re.I)


def _bounds(values: list[tuple[float, float]]) -> ObjectBounds | None:
    if not values:
        return None
    xs, ys = zip(*values, strict=True)
    return ObjectBounds(
        min_x_mm=round(min(xs), 3),
        min_y_mm=round(min(ys), 3),
        max_x_mm=round(max(xs), 3),
        max_y_mm=round(max(ys), 3),
    )


def _labelled_gcode_positions(data: bytes, *, plate_index: int | None) -> list[ObjectPosition]:
    """Extract slicer object labels and their extrusion-only XY footprints.

    This deliberately accepts several common comment spellings.  Unlabelled
    gcode cannot safely be divided into objects and returns no invented parts.
    """

    points: dict[str, list[tuple[float, float]]] = {}
    names: dict[str, str] = {}
    current: str | None = None
    x = y = None
    e = None
    absolute_e = True
    for raw in data.decode("latin-1", errors="replace").splitlines():
        label = _OBJECT_LABEL_RE.match(raw.strip())
        if label:
            name = label.group(1).strip()
            if name and name.lower() not in {"noname", "unknown"}:
                current = name
                points.setdefault(current, [])
                names[current] = name
            continue
        defined = _EXCLUDE_OBJECT_RE.match(raw.strip())
        if defined:
            name, cx, cy = defined.groups()
            current = name
            points.setdefault(name, []).append((float(cx), float(cy)))
            names[name] = name
            continue
        body = raw.split(";", 1)[0].strip()
        word = body.split(maxsplit=1)[0].upper() if body else ""
        if word == "M82":
            absolute_e = True
            continue
        if word == "M83":
            absolute_e = False
            continue
        if word not in {"G0", "G1"}:
            continue
        values = {axis: float(value) for axis, value in _AXIS_RE.findall(body.upper())}
        x, y = values.get("X", x), values.get("Y", y)
        extruder = _E_RE.search(body)
        is_extruding = False
        if extruder:
            next_e = float(extruder.group(1))
            is_extruding = next_e > (e if e is not None else 0.0) if absolute_e else next_e > 0
            e = next_e if absolute_e else (e or 0.0) + next_e
        if current and is_extruding and x is not None and y is not None:
            points[current].append((x, y))

    result: list[ObjectPosition] = []
    for index, (key, object_points) in enumerate(points.items(), start=1):
        bounds = _bounds(object_points)
        if bounds is None:
            continue
        result.append(ObjectPosition(
            object_id=f"gcode-{index}",
            name=names.get(key),
            center_x_mm=round((bounds.min_x_mm + bounds.max_x_mm) / 2, 3),
            center_y_mm=round((bounds.min_y_mm + bounds.max_y_mm) / 2, 3),
            bounds=bounds,
            plate_index=plate_index,
            source="gcode_label",
            gcode_bounds=bounds,
        ))
    return result


def _model_positions(
    data: bytes, *, plate_index: int | None, object_plates: dict[str, int] | None = None
) -> list[ObjectPosition]:
    """Read standard 3MF build-item transforms and object vertex bounds.

    The 3MF transform has nine linear values followed by translation.  The
    implementation handles ordinary mesh resources; unsupported components are
    omitted rather than guessed.
    """

    root = _parse_xml(data)
    if root is None:
        return []
    meshes: dict[str, list[tuple[float, float, float]]] = {}
    names: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag) != "object" or not element.get("id"):
            continue
        object_id = element.get("id") or ""
        name = element.get("name") or element.get("partnumber")
        if name:
            names[object_id] = name
        vertices: list[tuple[float, float, float]] = []
        for vertex in element.iter():
            if _local_name(vertex.tag) != "vertex":
                continue
            try:
                vertices.append((float(vertex.get("x", "")), float(vertex.get("y", "")), float(vertex.get("z", "0"))))
            except ValueError:
                continue
        if vertices:
            meshes[object_id] = vertices
    result: list[ObjectPosition] = []
    for item in root.iter():
        if _local_name(item.tag) != "item" or not item.get("objectid"):
            continue
        object_id = item.get("objectid") or ""
        vertices = meshes.get(object_id)
        if not vertices:
            continue
        try:
            transform = [float(value) for value in (item.get("transform") or "").split()]
        except ValueError:
            continue
        if not transform:
            transform = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
        if len(transform) != 12:
            continue
        a, b, _c, d, e, _f, _g, _h, _i, tx, ty, _tz = transform
        transformed = [(a * vx + d * vy + tx, b * vx + e * vy + ty) for vx, vy, _vz in vertices]
        bounds = _bounds(transformed)
        if bounds is None:
            continue
        rotation = round(__import__("math").degrees(__import__("math").atan2(b, a)), 3)
        result.append(ObjectPosition(
            object_id=f"3mf-{object_id}",
            name=names.get(object_id),
            center_x_mm=round((bounds.min_x_mm + bounds.max_x_mm) / 2, 3),
            center_y_mm=round((bounds.min_y_mm + bounds.max_y_mm) / 2, 3),
            bounds=bounds,
            rotation_deg=rotation,
            plate_index=(object_plates or {}).get(object_id, plate_index),
            source="model_transform",
        ))
    return result


def _model_setting_plates(data: bytes) -> dict[str, int]:
    """Map Bambu project object IDs to their declared plate, when present."""

    root = _parse_xml(data)
    if root is None:
        return {}
    plates: dict[str, int] = {}
    for plate in root.iter():
        if _local_name(plate.tag) != "plate":
            continue
        try:
            index = int(plate.get("index", ""))
        except ValueError:
            continue
        for object_ in plate.iter():
            if _local_name(object_.tag) == "object" and object_.get("id"):
                plates[object_.get("id") or ""] = index
    return plates


def inspect_object_positions(path: Path, *, scan_max_bytes: int) -> tuple[ObjectPosition, ...]:
    """Return object positions from a submitted artifact without printer I/O."""

    if path.suffix.lower() == ".gcode":
        data = path.read_bytes()
        if len(data) > scan_max_bytes:
            return ()
        return tuple(_labelled_gcode_positions(data, plate_index=None))
    if path.suffix.lower() != ".3mf":
        raise ArtifactError(f"unsupported artifact extension {path.suffix!r}")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        plate_names = sorted(name for name in names if _PLATE_GCODE_RE.match(name))
        plate_index = _plate_index(plate_names[0]) if plate_names else None
        model = _read_member(archive, "3D/3dmodel.model", budget=_MAX_CONFIG_BYTES)
        settings = _read_member(archive, "Metadata/model_settings.config", budget=_MAX_CONFIG_BYTES)
        positions = (
            _model_positions(
                model,
                plate_index=plate_index,
                object_plates=_model_setting_plates(settings) if settings else None,
            )
            if model else []
        )
        if plate_names:
            gcode = _read_member(archive, plate_names[0], budget=scan_max_bytes)
            labelled = _labelled_gcode_positions(gcode or b"", plate_index=plate_index)
            by_name = {entry.name: entry for entry in labelled if entry.name}
            positions = [
                entry.model_copy(update={"gcode_bounds": by_name[entry.name].bounds})
                if entry.name in by_name else entry
                for entry in positions
            ]
            if not positions:
                positions = labelled
        return tuple(positions)
