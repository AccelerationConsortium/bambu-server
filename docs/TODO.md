# TODO

## Live printer configuration

- The gitignored `printers.local.yaml` and `.env` configure one P1S and one H2D.
- Their addresses, LAN access codes, and serial numbers were obtained locally
  without recording credentials in the repository or chat.
- Both printers are reachable over local MQTT/TLS on TCP port `8883` while LAN
  Only Mode remains off, preserving cloud control.
- `/health`, `/printers`, and both `/status` endpoints return HTTP 200 with live
  MQTT telemetry.
- Since 2026-10-05 the live deployment runs with `dispatch.enabled` for the P1S.
  Its first start was ignored by the printer (firmware 01.10.00.00 in cloud
  mode most likely refuses LAN control). The plan to move the P1S to LAN Only
  + Developer Mode is `docs/LAN_ONLY_MIGRATION.md`.

The printer IP addresses and MAC addresses are intentionally not recorded here;
they are site-specific configuration and belong in the gitignored local files.

## Remote Bambu Studio through Tailscale (planned)

- Planned for the week of **2026-10-19**; requested 2026-10-12.
- Use the gateway host as a subnet router for the selected printer's `/32`
  route, then bind Bambu Studio by local IP and access code.
- Follow the [setup and rollback checklist](LAN_ONLY_MIGRATION.md#remote-bambu-studio-through-tailscale).
  Host Tailscale state and end-to-end connectivity still need verification;
  no network settings were changed. The existing P1S migration gates apply.

## Rich read-only telemetry (done)

- `details` now enriches a `data_ready` status with read-only observations:
  `print_type`, `nozzle_type`/`nozzle_diameter`, `wifi_signal`,
  `print_error_code` (appended to a failed job's `last_error.message`),
  `skipped_objects`, and `ams_trays` (loaded filament inventory; tray/tag UUIDs
  intentionally withheld).
- Each getter is isolated so no single failure can poison a poll; `activity` and
  the top-level state never depend on these.

## Control plane (built 2026-10-04, off by default)

Approved scope and departures are in `docs/CONTROL_PLANE_DESIGN.md` (status
block). Built: per-printer claims, `snapshot`, `start_print`, `stop_print`,
`allowed_actions` from the same gate as the 412s, STL slicing via the Bambu
Studio CLI, the `/ui` start flow with the empty-plate confirmation, restart
recovery that fails an interrupted dispatch with its uncertainty.

Still owed:

- **First live run.** Enable on the P1S only, declare `plate`, and start a small
  PLA cube with a person at the printer. Confirm the printer's `gcode_file`
  reports the uploaded name (the job is marked running on a state change even
  if it does not), and that the stored upload is visible on the printer's
  storage.
- **Camera on the H2D.** The snapshot speaks the P1-series camera protocol
  (port 6000). The H2D uses a different one; until it is supported the H2D's
  plate check is in person only.
- **Printer storage housekeeping.** Uploaded `gw_*.3mf` files stay on the
  printer's storage; nothing deletes them yet.
- **`lab-skills` integration** and the §6.4 human-gated reconcile verb.
- **Slicing is inline** in `POST /submissions`; a slow slice holds the request.

## A1 agent mode: part placement (owed, found 2026-11-04)

Found on the first agent-run A1 print (submission
`24dd23a8d8dd4707a986c21a734227dd`, B1-B2-B3 adaptor, started 2026-11-04
19:21 UTC). The person asked mid-print for the part to sit towards the front and
slightly left; no agent could do that.

- **The gateway slicer cannot place a part.** `slicer.py` runs Bambu Studio with
  `--arrange 1`, so an uploaded `.stl` lands wherever auto-arrange puts it
  (about centre). `POST /submissions` has no position or rotation field. The
  only way to place a part is to slice locally with A1 presets and submit the
  `.3mf`, which most agents cannot do.
- **The fit check is blind for these slices.** `build_fits_plate` returned
  `not_applicable` ("the toolpath uses relative positioning"), so an agent
  moving a part towards an edge would get no fit check.
- **Reading back is possible:** `GET /submissions/{id}/objects` already reports
  where each sliced object sits in bed coordinates (mm, origin front-left, +X
  right, +Y back). It is not exposed in the lab MCP yet.

Proposed (needs approval; a submission parameter, not a new printer verb, so it
stays inside the approved A1 agent-mode workflow):

1. Optional `center_x_mm`, `center_y_mm`, `rotation_deg` form fields on `.stl`
   submissions. When present, slice with `--arrange 0` and the part translated
   to that centre; refuse (422) a placement whose footprint leaves the declared
   `bed_size_mm`. Record the requested placement in `provenance`.
2. Make `build_fits_plate` use the slicer's `plate_N.json` object bounds when the
   toolpath is relative, so placement is actually checked.
3. Lab MCP (`ac-lab-agent/.ac-lab-agent/lab_mcp.py`, not under git yet): add the
   placement fields to `a1_submit` and an `a1_objects` tool so an agent confirms
   the position before `a1_approve`.

Also owed from the same session: `docs/API_REFERENCE.md` still says "Agents use
the SDK for printer I/O"; align it with the agent guide's *How printer I/O is
routed* (AGENTIC_LAB_DESIGN.md §1.3, ac-organic-lab#102).

## Submission pipeline (implemented, including dispatch)

See `docs/SUBMISSION_PIPELINE_DESIGN.md`. Built: submission intake
(`POST /submissions`, including `.stl` slicing), the machine-profile read
surface (`GET /printers/{id}/profile`), the eight-check validator, the job state
machine with a durable store, the per-machine queue with expected finish times
(`GET /printers/{id}/queue`), and dispatch through the control plane above.
The historical notes below describe decisions taken before dispatch existed.

Decisions taken where §12 left them open, all following the design's own
recommendation:

1. **Approval model — human-in-the-loop.** Nothing auto-approves;
   `POST /submissions/{id}/approve` records sign-off and sets
   `verdict.dispatch_ready`. Since dispatch is stubbed, this only shapes the
   gate, and relaxing it per machine stays available.
2. **Identity — opaque, superseded for control.** Without the edge, names are
   opaque labels. The control plane requires edge-verified identities for the
   approver and the person starting a print (`require_verified_identity`).
3. **Queue — gateway-owned.** Running remaining time comes from printer
   telemetry, queued durations from the artifact's own slicer estimate.
4. **`.3mf` parsing — standard library.** A sliced plate file embeds
   `Metadata/plate_N.gcode`, which is scanned like a bare `.gcode`; sidecar
   `project_settings.config` / `slice_info.config` fill in the rest. No mesh
   parsing and no new dependency, which also sidesteps the 3MF build-transform
   coordinate ambiguity — the footprint is measured as an extent.
5. **Agent-authorized runs — not permitted.** Follows (1).

Open, from the design's §10 data gaps and what the build surfaced:

- AMS parsing now uses allowlisted cached MQTT fields, without the library's
  required presence bits / `n` / complete spool-tag assumptions. Empty units,
  HT IDs, and remaining percentages are preserved. Material matching requires
  every material; known empty inventory fails rather than skipping the check.
  Still needed before dispatch: explicit per-filament tray mapping, per-slot
  temperature and quantity checks, and fresh revalidation under a claim.
- Machine profiles must be filled in per printer in `printers.local.yaml`
  (`bed_size_mm`, `limits`, `ams.filament_forbidden`). Until they are, the
  checks that need them report `not_applicable` rather than passing.
- `nozzle_type` is `None` on the H2D; declare it in that printer's profile so
  `machine_compatible` has something to compare.
- Validation runs inline on submission. A very large artifact makes the POST
  slow (the read is on a worker thread, so it does not stall status polls). If
  that becomes a problem, move it to an async worker — the state machine already
  has the `validating` state for it.
- **Beyond the design's state machine:** a `cancelled` terminal state and
  `POST /submissions/{id}/cancel` were added after the first live test left an
  unremovable job in the P1S queue. The contract's §5 declares no such state.
  It is legal only from `queued` / `approved`, never for a dispatched job —
  aborting a print stays a control-plane action. Worth folding back into
  `SUBMISSION_PIPELINE_DESIGN.md` §5 when that doc is next revised.
- ~~No retention policy~~ — done, then made opt-in (2026-11-02): records are
  print records, so nothing is swept unless `submissions.retain_terminal_days`
  is set, cancelling keeps the artifact, and result bundles are never swept.
  When set, terminal records are swept at startup past that age, and
  `DELETE /submissions/{id}` removes one finished job immediately. A job still
  in play is never swept however old. Sweeping at startup rather than on a
  timer keeps on-disk and in-memory views identical — the divergence that
  forced hand-cleanup during bring-up.

## Submission page

`GET /ui` — one static file (`src/bambu_server/static/index.html`), no build
step, no external resources, served from the same origin as the API. Added
because the pipeline shipped with no human-facing surface at all: the design
assumed the lab dashboard would render these endpoints, so a UI was never in
its scope, which left `curl` and Swagger as the only way in.

Deliberate limits: it holds no state, calls only public endpoints, and offers
Approve only on a `queued` job so it can never advertise a refusal. It has no
sign-in, matching the rest of the service.

Not visually verified — there is no browser on this host, so only the HTML
structure and the script's syntax were checked. Worth a look in a real browser
before pointing users at it. The durable home is probably the lab dashboard
(`ac-organic-lab/web`) once `ac_auth` makes `requested_by` a real identity;
this page is the interim surface.

## Edge identity / SSO (code done, deployment pending)

The submission page and its API now work behind the lab's single Caddy edge, so
a submission can be attributed to a signed-in person instead of a typed-in
label. What shipped:

- `identity.py` — trusts `X-Auth-User` only when `X-Edge-Auth` matches
  `BAMBU_EDGE_SHARED_SECRET` (constant-time), **fails closed** with no secret
  configured, and a verified identity overrides any client-supplied name.
- `requested_by_verified` / `approved_by_verified` on every job; the history
  note marks a verified actor.
- `GET /whoami` so the page can word itself honestly.
- The page derives its API base from its own URL (strips a trailing `/ui`), so
  one file serves the direct deployment and any edge prefix. Answers at both
  `/ui` and `/ui/` — a slash redirect would drop the edge prefix.
- `ac-organic-lab`: the `/bambu/*` route in `deploy/Caddyfile.single-edge`, and
  Utils → 3D Printers frames `/bambu/ui/`.

**Found while preparing the deploy (2026-09-07):**
`/etc/caddy/Caddyfile` has **diverged** from
`ac-organic-lab/deploy/Caddyfile.single-edge`. Production factored its routes
into a shared `(edge_routes)` snippet imported by two site blocks — an `http://`
address and an `https://` MagicDNS one, added when TLS was turned on — while the
repo copy still has the older single-`http://` layout. Copying the repo file
over the deployed one would silently drop the HTTPS site. `docs/EDGE_DEPLOY.md`
now applies the `/bambu/*` block *into* the deployed file instead. Reconciling
the repo copy with production is a separate task and belongs to whoever added
the HTTPS block, since only they know what else changed.

**Not deployed.** Three root steps, none of which I can do:

1. Install the updated `deploy/Caddyfile.single-edge` into `/etc/caddy` and
   reload Caddy.
2. Set the *same* `BAMBU_EDGE_SHARED_SECRET` in Caddy's systemd
   `EnvironmentFile` and in bambu-server's unit environment, then restart both.
   Until then the embed shows a blank frame and the gateway trusts nothing —
   both fail closed, which is why shipping this ahead of deployment is safe.
3. **Then** revert the bind to `127.0.0.1` (step 5 of the plan). It was widened
   to `0.0.0.0` so the page was reachable at all; once the edge fronts it, the
   loopback bind closes the unauthenticated `/submissions` path on the tailnet.
   Doing it before the route exists would break the working page.

Known gap, inherited from the OT-2 embed: a write inside the framed panel
bypasses the dashboard's `control_action` audit row. Submissions are recorded in
the job store's history, so there is a trail. Lifecycle events can now be
pushed with `agent_workflow.event_webhook_urls` (e.g. to `/api/ingest/events`)
and result bundles through the outbox to the central ingest
(`BAMBU_RESULTS_URL`); the central side of the print-results route, and of
`GET {url}/{device_id}/{id}` filing status, still has to exist there.

## Test suite

- `uv run ruff check .` passes.
- `uv run pytest -q` covers the FastAPI API tests and
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
