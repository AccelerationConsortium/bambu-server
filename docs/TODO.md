# TODO

## Live printer configuration

- The gitignored `printers.local.yaml` and `.env` configure one P1S and one H2D.
- Their addresses, LAN access codes, and serial numbers were obtained locally
  without recording credentials in the repository or chat.
- Both printers are reachable over local MQTT/TLS on TCP port `8883` while LAN
  Only Mode remains off, preserving cloud control.
- `/health`, `/printers`, and both `/status` endpoints return HTTP 200 with live
  MQTT telemetry.
- The gateway remains monitoring-only and exposes no control endpoints.

The printer IP addresses and MAC addresses are intentionally not recorded here;
they are site-specific configuration and belong in the gitignored local files.

## Test suite

- `uv run ruff check .` passes.
- `uv run pytest -q` passes all 11 tests, including the FastAPI API tests.
- The current FastAPI/Starlette stack emits a deprecation warning because
  Starlette's `TestClient` still uses `httpx`; track the upstream migration to
  `httpx2`, but it does not currently fail or hang the suite.

## Network check

- The server can reach both discovered printers over Wi-Fi.
- TCP port `8883` is open on both, consistent with Bambu LAN MQTT/TLS.
- This was a targeted reachability check; no broad campus-network scan was run.
