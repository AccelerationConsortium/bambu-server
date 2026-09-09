# Bambu gateway API

Use the authenticated dashboard-origin `/bambu/` prefix for browser access:
`/bambu/docs` (Swagger), `/bambu/openapi.json` (schema), `/bambu/ui/` (submission page).
The dashboard also publishes the live schema through
`/api/equipment/bambu_gateway/documentation/openapi.json` and a read-only Swagger
viewer at `/api/equipment/bambu_gateway/documentation/docs`.

## Printer inventory

`GET /printers` discovers configured printers. For each ID:

- `GET /printers/{id}/status` is the STATUS_SPEC v1.2 envelope; inventory is
  under `details.ams_trays` and `details.ams_unit_ids`.
- `GET /printers/{id}/profile` exposes typed `observed.loaded_trays` and
  `observed.ams_unit_ids`. Stale/unavailable observations are withheld.
- `GET /printers/{id}/queue` shows the observed running job and waiting jobs.

All paths above are relative to the gateway prefix. Reads use the background
monitor's cache and never initiate printer I/O.

| Tray field | Meaning |
|---|---|
| `ams_id`, `tray_id` | Printer-reported unit ID and zero-based slot; together they identify a tray. HT unit IDs are preserved. |
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

`POST /submissions` accepts an artifact and metadata, validates it, and queues
passing jobs. Approval and cancellation update gateway records only. There is
no dispatch endpoint, no hardware control route, and approval alone does not
establish a complete filament-to-slot mapping or sufficient remaining quantity.

The gateway trusts edge identity only with its configured shared edge secret.
Names supplied directly by clients are unverified labels. Dashboard links use
the authenticated edge; this does not turn the gateway's direct port into an
authenticated surface. Never send secrets or printer addresses in submissions.
