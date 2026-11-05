"""Local gateway configuration and secret resolution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class TemperatureBand(BaseModel):
    """An inclusive operating range.

    Accepts either the explicit mapping ``{min_c: 0, max_c: 300}`` or the
    shorthand sequence ``[0, 300]``, because a two-number range reads better as
    a pair in YAML and both forms show up in operator-written config.
    """

    min_c: float
    max_c: float

    @model_validator(mode="before")
    @classmethod
    def accept_pair(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("a temperature band must be [min_c, max_c]")
            return {"min_c": value[0], "max_c": value[1]}
        return value

    @model_validator(mode="after")
    def validate_order(self) -> TemperatureBand:
        if self.max_c <= self.min_c:
            raise ValueError("max_c must exceed min_c")
        return self

    def contains(self, value: float) -> bool:
        return self.min_c <= value <= self.max_c


class MachineLimits(BaseModel):
    """Operator-declared safe operating envelope for one machine.

    Every band is optional and there are deliberately no built-in defaults: a
    guessed limit is a fabricated machine fact, and the validator reports an
    undeclared band as *not applicable* rather than silently passing a check it
    could not actually perform.
    """

    nozzle_temperature_c: TemperatureBand | None = None
    bed_temperature_c: TemperatureBand | None = None
    chamber_temperature_c: TemperatureBand | None = None


class TrayColorLabel(BaseModel):
    """Local operator color declaration, guarded against a changed spool type/code."""

    ams_id: int = Field(ge=0)
    tray_id: int = Field(ge=0)
    material: str = Field(min_length=1, max_length=40)
    reported_color: str = Field(pattern=r"^#?[0-9a-fA-F]{8}$")
    name: str = Field(min_length=1, max_length=60)


class AmsPolicy(BaseModel):
    filament_forbidden: list[str] = Field(default_factory=list)
    color_labels: list[TrayColorLabel] = Field(default_factory=list)

    @field_validator("filament_forbidden")
    @classmethod
    def normalise(cls, value: list[str]) -> list[str]:
        return [item.strip().upper() for item in value if item.strip()]


class MachineProfileConfig(BaseModel):
    """The operator-declared half of a machine profile.

    Nozzle type and diameter are also reported live by the printer, but the live
    field is blank on some models (a dual-nozzle H2D reports no parsable nozzle
    type), so the declared value is kept as the authoritative fallback and
    machine-compatibility checking never depends on a blank live field.
    """

    enclosure: Literal["enclosed", "open"] | None = None
    nozzle_type: str | None = Field(default=None, max_length=60)
    nozzle_diameter_mm: float | None = Field(default=None, gt=0, le=2.0)
    bed_size_mm: tuple[float, float] | None = None
    #: The build plate physically installed, in the printer's own vocabulary
    #: (the ``bed_type`` a print command names). Declared, never observed: a
    #: P1S does not detect its plate, so this is the only place the fact
    #: exists. Dispatch refuses a model sliced for a different plate, and
    #: refuses outright while it is undeclared.
    plate: BedPlate | None = None
    chamber_temperature_c: float | None = None
    limits: MachineLimits = Field(default_factory=MachineLimits)
    ams: AmsPolicy = Field(default_factory=AmsPolicy)

    @field_validator("bed_size_mm")
    @classmethod
    def validate_bed(cls, value: tuple[float, float] | None) -> tuple[float, float] | None:
        if value is not None and (value[0] <= 0 or value[1] <= 0):
            raise ValueError("bed_size_mm entries must be positive")
        return value


#: Plate names as the printer's print command spells them. The slicer's
#: ``curr_bed_type`` strings map onto these in :mod:`bambu_server.plates`.
BedPlate = Literal["cool_plate", "eng_plate", "hot_plate", "textured_plate", "supertack_plate"]


class BedPositions(BaseModel):
    """Named Y positions the ``position_bed`` verb may park a bed-slinger at.

    Approved by Yang on 2026-11-05 for the Bambu A1 only. The caller names a
    position, never a coordinate: ``inspect`` is where the camera sees the
    whole plate (for the plate check), ``pickup`` where the robot arm takes
    parts off (keep it equal to the slicer's ``end_bed_y_mm``).
    """

    inspect_y_mm: float = Field(ge=0, le=256)
    pickup_y_mm: float = Field(ge=0, le=256)
    #: Feed rate of the move, mm/min. 3000 (50 mm/s) is gentler than the
    #: A1's own end-gcode park move (3600).
    feed_mm_min: int = Field(default=3000, ge=300, le=6000)

    def y_for(self, position: str) -> float:
        return self.inspect_y_mm if position == "inspect" else self.pickup_y_mm


class PrinterDefinition(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    env_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    profile: MachineProfileConfig = Field(default_factory=MachineProfileConfig)
    #: Whether an admin may switch agent mode on for this printer at runtime.
    #: Approved 2026-11-02 for the Bambu A1 only; refused on any other model.
    agent_mode_eligible: bool = False
    #: Named bed positions for ``position_bed`` (approved 2026-11-05 for the
    #: Bambu A1 only). Absent means the verb is refused on this printer.
    bed_positions: BedPositions | None = None

    @field_validator("id", "name", "model", "env_prefix", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def agent_mode_only_on_a1(self) -> PrinterDefinition:
        from .agent_mode import model_is_agent_mode_capable

        if self.agent_mode_eligible and not model_is_agent_mode_capable(self.model):
            raise ValueError(
                f"agent_mode_eligible is approved for Bambu A1 printers only, not {self.model!r}"
            )
        if self.bed_positions is not None and not model_is_agent_mode_capable(self.model):
            raise ValueError(
                f"bed_positions (position_bed) is approved for Bambu A1 printers only, "
                f"not {self.model!r}"
            )
        bed = self.profile.bed_size_mm
        if self.bed_positions is not None and bed is not None:
            for name in ("inspect_y_mm", "pickup_y_mm"):
                if getattr(self.bed_positions, name) > bed[1]:
                    raise ValueError(f"bed_positions.{name} is beyond the {bed[1]:g} mm bed")
        return self


class SubmissionSettings(BaseModel):
    """Submission intake limits and storage location.

    ``directory`` holds uploaded artifacts and their metadata. Keep it out of
    git: submitted models are user data, not repository content.
    """

    directory: Path = Path("var/submissions")
    max_file_bytes: int = Field(default=200 * 1024 * 1024, ge=1024, le=2 * 1024**3)
    # Above this size an artifact is scanned head-and-tail only: enough to read
    # a slicer config block (which sits at either end of the file) but not
    # enough to trust a motion-derived bounding box, so plate fit is reported
    # as not applicable rather than computed from a partial scan.
    scan_max_bytes: int = Field(default=64 * 1024 * 1024, ge=64 * 1024)
    #: Age after which a *terminal* job's record is swept at startup. Off by
    #: default: job records and their artifacts are the lab's print records.
    #: Jobs still in play are never swept however old they are. Result bundles
    #: under ``results/`` are never swept.
    retain_terminal_days: float | None = Field(default=None, gt=0)
    #: Delete the stored artifact when a waiting job is cancelled. Off by
    #: default so a withdrawn job can still be inspected and downloaded.
    cancel_deletes_artifact: bool = False


class DispatchSettings(BaseModel):
    """The control plane's switches. Off unless the local config turns it on.

    ``enabled: false`` is the development and test default (AGENT_RULES 5: a
    default must not be able to contact lab hardware). With it off the gateway
    behaves exactly as the monitoring-only release did: no claim routes, no
    control routes, ``allowed_actions`` empty.
    """

    enabled: bool = False
    #: Refuse dispatch and stop unless both the approver and the caller are
    #: edge-verified identities. A direct tailnet caller can still submit,
    #: but cannot start a printer. Only a test should turn this off.
    require_verified_identity: bool = True
    #: A print may only be started on a cold machine. Above either value the
    #: previous job has not finished cooling (or something is still heating),
    #: and the plate state is not trustworthy.
    safe_bed_c: float = Field(default=45.0, gt=0)
    safe_nozzle_c: float = Field(default=70.0, gt=0)
    #: How long after publishing the start command the printer has to report
    #: the job in flight before the dispatch is recorded as failed. Covers the
    #: P1S's file-prepare phase; a bed-leveling pass happens *after* PREPARE.
    confirm_timeout_s: float = Field(default=180.0, ge=10, le=1800)
    #: Edge roles allowed to flip a printer's agent mode switch.
    agent_mode_admin_roles: list[str] = Field(default_factory=lambda: ["admin"])
    #: Edge roles whose callers are treated as agents (as is any request with
    #: an ``X-Agent-Id`` header).
    agent_roles: list[str] = Field(default_factory=lambda: ["agent"])
    #: An ``agent_camera`` decision must rest on a frame at most this old.
    agent_snapshot_max_age_s: float = Field(default=120.0, ge=5, le=300)
    #: Printer-side pre-print steps, forwarded in the start command.
    bed_leveling: bool = True
    flow_calibration: bool = False
    vibration_calibration: bool = False
    timelapse: bool = False


class AgentWorkflowSettings(BaseModel):
    """Optional data integrations for orchestration agents; all are off by default."""

    #: Operator-configured HTTPS endpoints, e.g. the dashboard event ingest
    #: route.  Failed delivery is logged without a body and never affects a job.
    event_webhook_urls: list[str] = Field(default_factory=list)
    event_buffer_size: int = Field(default=1000, ge=10, le=10000)
    #: Temperature at or below which a finished bed counts as cooled for
    #: pickup. Advice for the arm agent, never a robot command.
    pickup_safe_bed_c: float = Field(default=45.0, gt=0)
    #: How long after ``job.finished`` to keep watching the cached bed
    #: temperature for ``job.cooled``. Not resumed after a restart.
    cooldown_watch_timeout_s: float = Field(default=4 * 3600.0, ge=1, le=86400)
    #: Vision remains advisory under the lab safety rules. It cannot satisfy
    #: the human plate-clear confirmation required by start_print.
    vision_enabled: bool = False
    #: A directory containing operator-captured empty-bed references named
    #: ``<printer_id>.jpg``. No reference produces ``unknown``, never ``clear``.
    empty_bed_reference_directory: Path | None = None


class ResultsSettings(BaseModel):
    """Durable result bundles and the optional central-ingest outbox.

    Devices never write to the ELN directly: bundles go only to the central
    dashboard's ``/api/ingest/*``, which holds the ELN credentials. Without an
    ingest URL and token every job stays ``local_only``.
    """

    #: Identifies this gateway in result bundles and events.
    gateway_id: str = Field(default="bambu_gateway", pattern=r"^[a-z][a-z0-9_]*$")
    #: Central ingest endpoint, e.g. ``https://<dashboard>/api/ingest/print-results``.
    #: ``BAMBU_RESULTS_URL`` overrides it; the bearer token is only ever read
    #: from ``BAMBU_RESULTS_TOKEN``.
    ingest_url: str | None = Field(default=None, pattern=r"^https?://")
    poll_interval_s: float = Field(default=60.0, ge=0.05, le=86400)
    retry_initial_s: float = Field(default=10.0, ge=0.05, le=3600)
    retry_max_s: float = Field(default=1800.0, ge=0.05, le=86400)
    request_timeout_s: float = Field(default=10.0, ge=0.5, le=120)
    worker_tick_s: float = Field(default=2.0, ge=0.05, le=60)


def _upper_material_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key).strip().upper(): preset for key, preset in value.items()}
    return value


class SlicerPrinterPresets(BaseModel):
    """The presets one printer slices with.

    Only ``machine`` is required. ``process`` and ``filaments`` fall back to the
    gateway-wide ``process_profile`` and ``filament_profiles`` when omitted;
    a printer whose kinematics differ from those defaults (an A1 against X1C
    presets) must name its own. A ``filaments`` map *replaces* the defaults
    rather than extending them, so a material the printer does not list is
    refused instead of being sliced with another machine's preset.
    """

    machine: str = Field(min_length=1)
    process: str | None = Field(default=None, min_length=1)
    filaments: dict[str, str] | None = None
    #: Where the stock end gcode's final park move leaves the bed, as the Y
    #: axis position in bed millimetres. Unset keeps the stock move (the A1's
    #: ``G1 X-48 Y180``). On a bed-slinger a larger Y brings the plate further
    #: toward the front for pickup; stay short of the printable area's edge.
    end_bed_y_mm: float | None = Field(default=None, ge=0, le=1000)

    @field_validator("filaments", mode="before")
    @classmethod
    def upper_keys(cls, value: Any) -> Any:
        return _upper_material_keys(value)


class SlicerSettings(BaseModel):
    """Bambu Studio CLI slicing of an uploaded ``.stl``.

    Off by default: slicing shells out to an installed Bambu Studio, which is a
    machine-local fact. ``executable`` is the launcher; ``profiles_dir`` is
    Bambu Studio's bundled ``resources/profiles/BBL`` directory, where the
    machine, process and filament presets live; ``work_dir`` is where the
    slicer may write (the service runs with a read-only home).
    """

    enabled: bool = False
    executable: Path = Path("bambu-studio")
    profiles_dir: Path | None = None
    work_dir: Path = Path("var/slicer")
    timeout_s: float = Field(default=600.0, ge=30, le=3600)
    #: The default process preset, for a printer that does not name its own.
    #: One per printer, deliberately: the submitter picks a material, not a
    #: print profile.
    process_profile: str = "0.20mm Standard @BBL X1C"
    #: Default material name (as the submitter types it, upper-cased) to the
    #: filament preset to slice with, for a printer that does not name its own.
    #: A material with no entry cannot be sliced here.
    filament_profiles: dict[str, str] = Field(default_factory=dict)
    #: Printer id to its presets. A bare string is the machine preset alone
    #: (e.g. "Bambu Lab P1S 0.4 nozzle"), with the defaults above.
    #: A printer with no entry does not accept ``.stl`` uploads.
    machine_profiles: dict[str, SlicerPrinterPresets] = Field(default_factory=dict)
    #: Ceiling on an uploaded mesh.
    max_stl_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    #: Gap, in millimetres, between a ``front_left`` part's footprint and the
    #: front and left edges of the printable area: room for a brim or skirt.
    placement_margin_mm: float = Field(default=10.0, ge=0, le=100)

    @field_validator("filament_profiles", mode="before")
    @classmethod
    def upper_keys(cls, value: Any) -> Any:
        return _upper_material_keys(value)

    @field_validator("machine_profiles", mode="before")
    @classmethod
    def accept_bare_machine(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                printer_id: {"machine": presets} if isinstance(presets, str) else presets
                for printer_id, presets in value.items()
            }
        return value

    def presets_for(self, printer_id: str) -> SlicerPrinterPresets | None:
        return self.machine_profiles.get(printer_id)

    def process_for(self, printer_id: str) -> str:
        presets = self.presets_for(printer_id)
        if presets is not None and presets.process is not None:
            return presets.process
        return self.process_profile

    def filaments_for(self, printer_id: str) -> dict[str, str]:
        presets = self.presets_for(printer_id)
        if presets is not None and presets.filaments is not None:
            return presets.filaments
        return self.filament_profiles


class Settings(BaseModel):
    printers: list[PrinterDefinition] = Field(min_length=1)
    poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    stale_after_seconds: float = Field(default=20.0, ge=2.0, le=600.0)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    submissions: SubmissionSettings = Field(default_factory=SubmissionSettings)
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)
    agent_workflow: AgentWorkflowSettings = Field(default_factory=AgentWorkflowSettings)
    results: ResultsSettings = Field(default_factory=ResultsSettings)
    slicer: SlicerSettings = Field(default_factory=SlicerSettings)

    @model_validator(mode="after")
    def validate_unique_printers(self) -> Settings:
        ids = [printer.id for printer in self.printers]
        prefixes = [printer.env_prefix for printer in self.printers]
        if len(ids) != len(set(ids)):
            raise ValueError("printer ids must be unique")
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("printer env_prefix values must be unique")
        if self.stale_after_seconds <= self.poll_interval_seconds:
            raise ValueError("stale_after_seconds must exceed poll_interval_seconds")
        return self


class PrinterCredentials(BaseModel):
    host: SecretStr
    access_code: SecretStr
    serial: SecretStr


#: Env var holding the secret the lab's Caddy edge presents on every proxied
#: request. Set the *same* value here and in the edge's EnvironmentFile.
EDGE_SECRET_ENV = "BAMBU_EDGE_SHARED_SECRET"


def resolve_edge_secret() -> str | None:
    """The shared secret that lets this service trust an injected identity.

    Read from the environment, never from the YAML: it is a credential, and
    `printers.local.yaml` is a config file people paste into issues. Absent
    means no identity is ever trusted (see :mod:`bambu_server.identity`).
    """

    return (os.getenv(EDGE_SECRET_ENV) or "").strip() or None


RESULTS_URL_ENV = "BAMBU_RESULTS_URL"
RESULTS_TOKEN_ENV = "BAMBU_RESULTS_TOKEN"


def resolve_results_ingest(settings: ResultsSettings) -> tuple[str | None, str | None]:
    """The central ingest URL and bearer token, or ``None`` for either.

    The token is a credential and is read from the environment only.
    """

    url = (os.getenv(RESULTS_URL_ENV) or "").strip() or settings.ingest_url
    if url and not url.startswith(("http://", "https://")):
        raise ValueError(f"{RESULTS_URL_ENV} must be an http(s) URL")
    token = (os.getenv(RESULTS_TOKEN_ENV) or "").strip() or None
    return (url.rstrip("/") if url else None), token


def resolve_credentials(printer: PrinterDefinition) -> PrinterCredentials:
    names = {
        "host": f"{printer.env_prefix}_HOST",
        "access_code": f"{printer.env_prefix}_ACCESS_CODE",
        "serial": f"{printer.env_prefix}_SERIAL",
    }
    missing = [env_name for env_name in names.values() if not os.getenv(env_name)]
    if missing:
        raise ValueError(
            f"missing environment variables for printer {printer.id}: {', '.join(missing)}"
        )
    return PrinterCredentials(**{key: os.environ[name] for key, name in names.items()})


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        path = os.getenv("BAMBU_SERVER_CONFIG", "printers.local.yaml")
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"Bambu server config not found at {resolved}; copy printers.example.yaml "
            "to printers.local.yaml and configure .env"
        )

    load_dotenv(resolved.parent / ".env", override=False)
    with resolved.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"configuration at {resolved} must contain a YAML mapping")
    settings = Settings.model_validate(payload)
    # A relative submission directory is resolved against the config file, not
    # the process working directory, so a systemd unit and an interactive shell
    # agree on where submitted artifacts live.
    if not settings.submissions.directory.is_absolute():
        settings.submissions.directory = (
            resolved.parent / settings.submissions.directory
        ).resolve()
    if not settings.slicer.work_dir.is_absolute():
        settings.slicer.work_dir = (resolved.parent / settings.slicer.work_dir).resolve()
    references = settings.agent_workflow.empty_bed_reference_directory
    if references is not None and not references.is_absolute():
        settings.agent_workflow.empty_bed_reference_directory = (
            resolved.parent / references
        ).resolve()
    return settings
