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


class PrinterDefinition(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    env_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    profile: MachineProfileConfig = Field(default_factory=MachineProfileConfig)

    @field_validator("id", "name", "model", "env_prefix", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


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
    #: Age after which a *terminal* job's record is swept at startup. Jobs that
    #: are still in play are never swept however old they are: a job stuck in
    #: `validating` is a signal, not litter. Set to null to keep everything.
    retain_terminal_days: float | None = Field(default=30.0, gt=0)


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
    #: Printer-side pre-print steps, forwarded in the start command.
    bed_leveling: bool = True
    flow_calibration: bool = False
    vibration_calibration: bool = False
    timelapse: bool = False


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
    #: The process preset applied to every slice. One, deliberately: the
    #: submitter picks a material, not a print profile.
    process_profile: str = "0.20mm Standard @BBL X1C"
    #: Material name (as the submitter types it, upper-cased) to the filament
    #: preset to slice with. A material with no entry cannot be sliced here.
    filament_profiles: dict[str, str] = Field(default_factory=dict)
    #: Printer id to the machine preset for it (e.g. "Bambu Lab P1S 0.4 nozzle").
    #: A printer with no entry does not accept ``.stl`` uploads.
    machine_profiles: dict[str, str] = Field(default_factory=dict)
    #: Ceiling on an uploaded mesh.
    max_stl_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)

    @field_validator("filament_profiles", mode="before")
    @classmethod
    def upper_keys(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key).strip().upper(): preset for key, preset in value.items()}
        return value


class Settings(BaseModel):
    printers: list[PrinterDefinition] = Field(min_length=1)
    poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    stale_after_seconds: float = Field(default=20.0, ge=2.0, le=600.0)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    submissions: SubmissionSettings = Field(default_factory=SubmissionSettings)
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)
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
    return settings
