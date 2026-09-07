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


class AmsPolicy(BaseModel):
    filament_forbidden: list[str] = Field(default_factory=list)

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
    chamber_temperature_c: float | None = None
    limits: MachineLimits = Field(default_factory=MachineLimits)
    ams: AmsPolicy = Field(default_factory=AmsPolicy)

    @field_validator("bed_size_mm")
    @classmethod
    def validate_bed(cls, value: tuple[float, float] | None) -> tuple[float, float] | None:
        if value is not None and (value[0] <= 0 or value[1] <= 0):
            raise ValueError("bed_size_mm entries must be positive")
        return value


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


class Settings(BaseModel):
    printers: list[PrinterDefinition] = Field(min_length=1)
    poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    stale_after_seconds: float = Field(default=20.0, ge=2.0, le=600.0)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])
    submissions: SubmissionSettings = Field(default_factory=SubmissionSettings)

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
    return settings
