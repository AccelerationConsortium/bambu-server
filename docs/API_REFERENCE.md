# Bambu gateway API

Use the authenticated dashboard-origin `/bambu/` prefix for browser access:
`/bambu/docs` (Swagger), `/bambu/openapi.json` (schema), `/bambu/ui/` (submission page).
The dashboard also publishes the live schema through
`/api/equipment/bambu_gateway/documentation/openapi.json` and a read-only Swagger
viewer at `/api/equipment/bambu_gateway/documentation/docs`.

The served [agent guide](../src/bambu_server/docs/AGENT_GUIDE.md) is available
at `/agent-docs`; `/agent-docs/api-reference` generates the full method/path,
parameter, request, response and shared-schema reference from the running
app's OpenAPI. `/llms.txt` indexes the documentation, and `/docs/agent` exposes
the service-level JSON guide for SDK discovery as `bambu_gateway`. The
dashboard registers these under the same documentation proxy prefix above.
Swagger is read-only; the documentation proxy cannot execute commands.

## Endpoint summary

All paths are relative to the gateway. `{id}` denotes a printer or submission
ID as indicated by the prefix. The generated reference is authoritative for
the routes enabled in a deployment.

| Purpose | Methods and paths |
|---|---|
| Discovery and health | `GET /`, `/health`, `/status`, `/whoami`, `/printers` |
| Per-printer reads | `GET /printers/{id}/`, `/health`, `/status`, `/profile`, `/queue` |
| Submit and review | `POST /submissions`; `GET /submissions`, `/submissions/{id}`; `POST /submissions/{id}/approve`, `/cancel` |
| Claims (dispatch enabled) | `POST /printers/{id}/control/claim`, `/heartbeat`, `/release` |
| Printer I/O (dispatch enabled) | `POST /printers/{id}/control/snapshot`, `/light`, `/start_print`, `/stop_print` |
| View-only camera (dispatch enabled) | `GET /printers/{id}/camera.jpg` |
| Agent switch | `GET /printers/{id}/agent-mode`; `POST` on that path is admin-only, never agent-operated |
| Completion and pickup | `GET /events`, `/submissions/{id}/objects`, `/submissions/{id}/pickup-targets` |
| Durable records | `GET /submissions/{id}/artifact`, `/result`, `/plate.jpg`; `DELETE /submissions/{id}` for terminal-job housekeeping |
| Optional ELN delivery | `GET` / `POST /submissions/{id}/eln-ingest`; `GET /results/outbox` |
| Optional vision advisory | `POST /printers/{id}/bed-check` (dispatch and vision enabled) |
| Documentation | `GET /docs`, `/openapi.json`, `/agent-docs`, `/agent-docs/api-reference`, `/docs/agent`, `/llms.txt` |

Agents use the SDK for printer I/O. An occupied or unclear claimed snapshot
means release the claim and stop; never send a start request to test the gate.
Failed or uncertain dispatches require human reconciliation, without retries.

## Printer inventory

`GET /printers` discovers configured printers. For each ID:

- `GET /printers/{id}/status` is the STATUS_SPEC v1.2 envelope; inventory is
  under `details.ams_trays` and `details.ams_unit_ids`, and the external spool,
  when a filament is selected for it, under `details.external_spool` (same tray
  fields, `ams_id` 254, `tray_id` 0; never counted in `ams_trays`).
- `GET /printers/{id}/profile` exposes typed `observed.loaded_trays` and
  `observed.ams_unit_ids`. Stale/unavailable observations are withheld.
- `GET /printers/{id}/queue` shows the observed running job and waiting jobs.

All paths above are relative to the gateway prefix. Reads use the background
monitor's cache and never initiate printer I/O.

With `dispatch.enabled`, `GET /printers/{id}/camera.jpg` returns the latest
chamber camera frame (`image/jpeg`, `Cache-Control: no-store`, capture time in
`X-Captured-At`) without a claim. It is the one read that touches the printer:
the camera is read at most once per printer every 2 s and the frame is shared by
all callers; 503 when the camera does not deliver one. A frame from here is
never plate-check evidence. A person's camera `start_print` records a fresh
frame when no `snapshot_token` is sent, or uses a claimed `/control/snapshot`
no older than 5 minutes. An agent must supply its own claimed snapshot and
visual decision; the agent age limit is normally 120 seconds.

| Tray field | Meaning |
|---|---|
| `ams_id`, `tray_id` | Printer-reported unit ID and zero-based slot; together they identify a tray. HT unit IDs are preserved; 254/0 is the external spool, which `observed.loaded_trays` includes. |
| `tray_index` | Legacy nullable field. Do not use MQTT `n` as a slot index. |
| `tray_type` | Reported material, such as PLA, PETG, PP, or PC. |
| `tray_color` | Reported RGB/RGBA hex, retained even when a display label is declared. |
| `tray_color_name` | Human-readable color label; does not establish spool brand. |
| `tray_color_source` | `bambu_color_match`, `operator_declared`, `generic`, or `unknown`. |
| `remaining_percent` | Printer estimate from 0–100, or null when unavailable. |

Profile temperature fields are `nozzle_temp_min_c` / `nozzle_temp_max_c`;
status tray fields retain `nozzle_temp_min` / `nozzle_temp_max`.
Known empty inventory is an empty list; unavailable inventory is not proof that
the AMS is empty. `ams_unit_ids` preserves known empty units.

An all-zero reported color is unknown by default. An operator may declare a
display label in local `profile.ams.color_labels`; it matches unit, slot,
material, and reported color. Its source remains `operator_declared`, and it
does not affect validation or establish physical spool identity after a swap.

## Submission boundary and authentication

`POST /submissions` accepts an artifact (`.3mf`, `.gcode`, or `.stl` where
slicing is enabled for the target; `.stl` requires `material`) and metadata,
validates it, and queues passing jobs. A sliced job carries `provenance`.
Approval and cancellation update gateway records only, and approval alone does
not establish a filament-to-slot mapping or sufficient remaining quantity.

## Control (only when the deployment enables dispatch)

Per printer, under `/printers/{id}/control/`: `claim`, `heartbeat`, `release`
(STATUS_SPEC §5), then `snapshot`, `light` (`{"on": true|false}`),
`start_print`, `stop_print`, each requiring
`X-Claim-Token` (423 without it). `start_print` takes the approved
`submission_id`, `plate_confirmed_empty: true`, `plate_check_method`
(`printer_camera` with a `snapshot_token`, `in_person`, or — for an agent on a
printer in agent mode — `agent_camera`), and an `ams_mapping` of
`{filament_id, ams_id, tray_id}`. Refusals are 412 with a body naming the
failed gate; `allowed_actions` on `/status` mirrors them, and
`details.claimed_by` names the holder. `GET /submissions/{id}/plate.jpg` returns
the camera frame a start was confirmed against. Without dispatch enabled the
control routes are absent; the stored plate-evidence route remains registered.

**Camera snapshot for a plate check.** `POST /printers/{id}/control/snapshot`
with the claim token returns `{token, printer_id, captured_at, sha256,
content_type: "image/jpeg", image_base64}`. The token is valid for that printer
and that claim session only. A person's camera start accepts it for 5 minutes;
an agent's `agent_camera` start for `dispatch.agent_snapshot_max_age_s` (120 s
by default). H2D snapshots are not supported yet: the route answers 501 without
contacting the printer, so an H2D plate check is in person.

The gateway trusts edge identity only with its configured shared edge secret.
Names supplied directly by clients are unverified labels. Dashboard links use
the authenticated edge; this does not turn the gateway's direct port into an
authenticated surface. Never send secrets or printer addresses in submissions.

## Agent mode (Bambu A1 only)

Approved 2026-11-02 for `bambu_a1_01`. An agent is a request carrying
`X-Agent-Id`, or whose edge-verified role is in `dispatch.agent_roles`
(default `agent`). Agents may submit, approve and start only on a printer with
`agent_mode_eligible: true` (config accepts it on a Bambu A1 only) whose switch
is on; otherwise 403 with `agent_mode: "not_eligible"` or `"off"`.

- `GET /printers/{id}/agent-mode` — `{printer_id, eligible, enabled,
  changed_by, changed_by_role, changed_at, reason, audit: [...]}`.
- `POST /printers/{id}/agent-mode` `{"enabled": bool, "reason": "..."}` —
  edge-verified identities with a role in `dispatch.agent_mode_admin_roles`
  (default `admin`) only, never an agent; 409 for an ineligible printer. Each
  flip is appended to the audit log before it takes effect.
- `details.agent_mode` on `/printers/{id}/status`, and per printer on
  `/status`: `{eligible, enabled, changed_by, changed_at, reason}`.

An agent's start uses `plate_check_method: agent_camera`, a fresh
`snapshot_token` from this printer and claim, and optionally
`plate_check_rationale`. The dispatch record's `plate_check` then has
`decided_by: "agent"`, `agent_id`, `rationale` and `snapshot_sha256`, and the
frame is archived at `/submissions/{id}/plate.jpg`.

## Print records and ELN ingest

- `GET /submissions/{id}/artifact` streams the exact stored file with `ETag:
  "<sha256>"` (304 on `If-None-Match`; 410 once deleted).
- `GET /submissions/{id}/result` returns the result bundle (`schema_id`
  `ac.bambu.print_result`, `schema_version` 1, `final` once terminal). It
  survives `DELETE` of the job record.
- `POST /submissions/{id}/eln-ingest` `{"eln_project": "...", "requested_by"?}`
  queues a terminal job for filing through the central server's ingest; `GET`
  on the same path reads its outbox entry; `GET /results/outbox` lists them.
  States: `local_only`, `pending`, `delivered`, `filing`, `filed`, `held`.
- `POST /submissions` and `POST /submissions/{id}/approve` take an optional
  `eln_project`.
- `GET /submissions` adds `updated_after`, `order=newest` and `offset` for
  change paging; the default order is unchanged.

The gateway never writes to the ELN; only the central server does. `/status`
reports `results_ingest_configured`, `results_outbox`, `results_pending`,
`results_filing` and `results_held`.

## Agent workflow data

`GET /submissions/{id}/objects` returns where each sliced object sits on the
plate in **bed coordinates**: millimetres, origin at the front-left corner of
the printable area, +X right, +Y toward the back seen from the front, Z up, as
the printer's own toolpath addresses it (compare with the profile's
`bed_size_mm`). Each object has `object_id`, `name`, `center_x_mm`,
`center_y_mm`, `bounds`, `rotation_deg` (when known), `plate_index`, `source`
(`slicer_plate_json`, `gcode_label` or `model_transform`) and `gcode_bounds`,
the labelled-extrusion cross-check. No invented positions: an empty list when
the file does not say. There is no robot frame; the arm agent owns calibration.

`GET /submissions/{id}/pickup-targets` returns those objects with
`bed_size_mm`, `pickup: {bed_temperature_c, safe_bed_temperature_c, cooled,
observed_at}` from the monitor's cache, and `ready` (finished, cooled, and at
least one object). It never contacts a printer or a robot.

`GET /events?after=<event_id>` is a pollable lifecycle feed: `job.started`,
`job.finished`, `job.failed`, `job.rejected`, `job.cancelled`, `job.cooled`
and `gateway.restarted`, each with `schema_version`, `event_id`,
`gateway_id`, `boot_id`, `submission_id`, `printer_id`, `state`,
`updated_at`, `eln_project`, `note`, and for job events the bed-frame
`object_positions`; `job.finished` and `job.cooled` add `pickup`. Ids persist
across restarts, and a restart-recovered failure is a `job.failed` with
`recovered_after_restart: true`. Configure `agent_workflow.event_webhook_urls`
to POST the same JSON elsewhere; delivery is best effort and never affects a
job.

When both dispatch and `agent_workflow.vision_enabled` are enabled,
`POST /printers/{id}/bed-check` accepts a claimed-control `snapshot_token` and
returns an advisory empty-bed assessment based on an operator reference image.
It performs no camera access itself, and its result is not a plate check.
