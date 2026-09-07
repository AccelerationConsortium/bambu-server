from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from bambu_server.config import (
    PrinterDefinition,
    Settings,
    load_settings,
    resolve_credentials,
)


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="printer ids must be unique"):
        Settings.model_validate(
            {
                "printers": [
                    {"id": "same", "name": "One", "env_prefix": "ONE"},
                    {"id": "same", "name": "Two", "env_prefix": "TWO"},
                ]
            }
        )


def test_missing_credentials_name_variables_without_leaking_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = PrinterDefinition(id="bambu_one", name="One", env_prefix="BAMBU_ONE")
    monkeypatch.delenv("BAMBU_ONE_HOST", raising=False)
    monkeypatch.delenv("BAMBU_ONE_ACCESS_CODE", raising=False)
    monkeypatch.delenv("BAMBU_ONE_SERIAL", raising=False)
    with pytest.raises(ValueError) as exc_info:
        resolve_credentials(printer)
    assert "BAMBU_ONE_HOST" in str(exc_info.value)
    assert "BAMBU_ONE_ACCESS_CODE" in str(exc_info.value)
    assert "BAMBU_ONE_SERIAL" in str(exc_info.value)


def test_a_printer_without_a_profile_stays_valid() -> None:
    """Existing configuration files predate the machine profile block."""
    settings = Settings.model_validate(
        {"printers": [{"id": "bambu_one", "name": "One", "env_prefix": "BAMBU_ONE"}]}
    )
    profile = settings.printers[0].profile

    assert profile.bed_size_mm is None
    assert profile.limits.nozzle_temperature_c is None
    assert profile.ams.filament_forbidden == []


def test_temperature_bands_accept_a_pair_or_a_mapping() -> None:
    settings = Settings.model_validate(
        {
            "printers": [
                {
                    "id": "bambu_one",
                    "name": "One",
                    "env_prefix": "BAMBU_ONE",
                    "profile": {
                        "bed_size_mm": [256, 256],
                        "limits": {
                            "nozzle_temperature_c": [0, 300],
                            "bed_temperature_c": {"min_c": 0, "max_c": 110},
                        },
                        "ams": {"filament_forbidden": [" abs ", "asa"]},
                    },
                }
            ]
        }
    )
    profile = settings.printers[0].profile

    assert profile.bed_size_mm == (256.0, 256.0)
    assert profile.limits.nozzle_temperature_c.max_c == 300
    assert profile.limits.bed_temperature_c.min_c == 0
    # Forbidden materials are normalised so a check never fails on casing.
    assert profile.ams.filament_forbidden == ["ABS", "ASA"]


def test_an_inverted_temperature_band_is_rejected() -> None:
    with pytest.raises(ValidationError, match="max_c must exceed min_c"):
        Settings.model_validate(
            {
                "printers": [
                    {
                        "id": "bambu_one",
                        "name": "One",
                        "env_prefix": "BAMBU_ONE",
                        "profile": {"limits": {"bed_temperature_c": [110, 0]}},
                    }
                ]
            }
        )


def test_a_relative_submission_directory_resolves_against_the_config_file(
    tmp_path: Path,
) -> None:
    """A systemd unit and an interactive shell must agree on where files land."""
    config = tmp_path / "printers.yaml"
    config.write_text(
        "submissions:\n"
        "  directory: var/submissions\n"
        "printers:\n"
        "  - id: bambu_one\n"
        "    name: One\n"
        "    env_prefix: BAMBU_ONE\n",
        encoding="utf-8",
    )

    settings = load_settings(config)
    assert settings.submissions.directory == tmp_path / "var" / "submissions"
