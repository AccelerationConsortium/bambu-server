# Bambu gateway API

Use the authenticated dashboard-origin `/bambu/` prefix for browser access:
`/bambu/docs` (Swagger), `/bambu/openapi.json` (schema), `/bambu/ui/` (submission page).
The dashboard also publishes the live schema through
`/api/equipment/bambu_gateway/documentation/openapi.json` and a read-only Swagger
viewer at `/api/equipment/bambu_gateway/documentation/docs`.

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
never plate-check evidence: a camera `start_print` records its own frame under
the claim (a fresh capture when no `snapshot_token` is sent, or a
`/control/snapshot` taken under the claim within 5 minutes).

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
(`printer_camera` with a `snapshot_token`, or `in_person`), and an
`ams_mapping` of `{filament_id, ams_id, tray_id}`. Refusals are 412 with a body
naming the failed gate; `allowed_actions` on `/status` mirrors them, and
`details.claimed_by` names the holder. `GET /submissions/{id}/plate.jpg` returns
the camera frame a start was confirmed against. Without dispatch enabled none
of these paths exist.

The gateway trusts edge identity only with its configured shared edge secret.
Names supplied directly by clients are unverified labels. Dashboard links use
the authenticated edge; this does not turn the gateway's direct port into an
authenticated surface. Never send secrets or printer addresses in submissions.
