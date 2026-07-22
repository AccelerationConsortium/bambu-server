"""Local gateway configuration and secret resolution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class PrinterDefinition(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    env_prefix: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")

    @field_validator("id", "name", "model", "env_prefix", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class Settings(BaseModel):
    printers: list[PrinterDefinition] = Field(min_length=1)
    poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)
    stale_after_seconds: float = Field(default=20.0, ge=2.0, le=600.0)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:8000"])

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
    return Settings.model_validate(payload)
