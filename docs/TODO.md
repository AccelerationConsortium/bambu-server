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

## Rich read-only telemetry (done)

- `details` now enriches a `data_ready` status with read-only observations:
  `print_type`, `nozzle_type`/`nozzle_diameter`, `wifi_signal`,
  `print_error_code` (appended to a failed job's `last_error.message`),
  `skipped_objects`, and `ams_trays` (loaded filament inventory; tray/tag UUIDs
  intentionally withheld).
- Each getter is isolated so no single failure can poison a poll; `activity` and
  the top-level state never depend on these.

## Control plane (proposal, awaiting review)

See `docs/CONTROL_PLANE_DESIGN.md`. The gateway stays read-only until that
design is approved; no `/control/*` route ships without the v1.1 claim protocol,
per-action preconditions, and human-approval gating it specifies.

## Submission pipeline (implemented up to the approval gate)

See `docs/SUBMISSION_PIPELINE_DESIGN.md`. Built: submission intake
(`POST /submissions`), the machine-profile read surface
(`GET /printers/{id}/profile`), the eight-check validator, the job state
machine with a durable store, and the per-machine queue with expected finish
times (`GET /printers/{id}/queue`).

Dispatch is **not** implemented. `submissions.dispatch()` raises
`DispatchUnavailable`, no HTTP route calls it, and no route can enter
`dispatching` / `running` / `finished`. No `/control/*` route exists.

Decisions taken where §12 left them open, all following the design's own
recommendation:

1. **Approval model — human-in-the-loop.** Nothing auto-approves;
   `POST /submissions/{id}/approve` records sign-off and sets
   `verdict.dispatch_ready`. Since dispatch is stubbed, this only shapes the
   gate, and relaxing it per machine stays available.
2. **Identity — opaque.** `ac_auth` is not wired here, so `requested_by` and
   `approved_by` are opaque strings recorded in job history. They are **not**
   authentication; network-layer gating is unchanged.
3. **Queue — gateway-owned.** Running remaining time comes from printer
   telemetry, queued durations from the artifact's own slicer estimate.
4. **`.3mf` parsing — standard library.** A sliced plate file embeds
   `Metadata/plate_N.gcode`, which is scanned like a bare `.gcode`; sidecar
   `project_settings.config` / `slice_info.config` fill in the rest. No mesh
   parsing and no new dependency, which also sidesteps the 3MF build-transform
   coordinate ambiguity — the footprint is measured as an extent.
5. **Agent-authorized runs — not permitted.** Follows (1).

Open, from the design's §10 data gaps and what the build surfaced:

- `ams_trays` is still `None` on both live printers, so `material_filament_match`
  and the tray half of `nozzle_temp_in_band` report `not_applicable`. The
  backend now reads each tray's `nozzle_temp_min` / `nozzle_temp_max`, so both
  checks become real the moment tray data appears — no code change needed.
- Machine profiles must be filled in per printer in `printers.local.yaml`
  (`bed_size_mm`, `limits`, `ams.filament_forbidden`). Until they are, the
  checks that need them report `not_applicable` rather than passing.
- `nozzle_type` is `None` on the H2D; declare it in that printer's profile so
  `machine_compatible` has something to compare.
- Validation runs inline on submission. A very large artifact makes the POST
  slow (the read is on a worker thread, so it does not stall status polls). If
  that becomes a problem, move it to an async worker — the state machine already
  has the `validating` state for it.
- No cancel/withdraw path exists for a queued job; the contract's state machine
  declares none.
- No retention policy: rejected and finished jobs, and their uploaded artifacts,
  stay on disk and in `GET /submissions` indefinitely. Fine at current volume,
  but it needs a sweep before this runs unattended for long.

## Test suite

- `uv run ruff check .` passes.
- `uv run pytest -q` passes all 114 tests, including the FastAPI API tests and
  the submission pipeline (artifact inspection, validation, store/state machine,
  queue ETA, HTTP surface). Tests build their own `.3mf` and `.gcode` fixtures
  and use fake backends; nothing touches hardware.
- The current FastAPI/Starlette stack emits a deprecation warning because
  Starlette's `TestClient` still uses `httpx`; track the upstream migration to
  `httpx2`, but it does not currently fail or hang the suite.

## Network check

- The server can reach both discovered printers over Wi-Fi.
- TCP port `8883` is open on both, consistent with Bambu LAN MQTT/TLS.
- This was a targeted reachability check; no broad campus-network scan was run.
