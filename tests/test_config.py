from __future__ import annotations

import pytest
from pydantic import ValidationError

from bambu_server.config import PrinterDefinition, Settings, resolve_credentials


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
