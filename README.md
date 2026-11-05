# AC Bambu Server

A FastAPI gateway for the Bambu Lab printers in the lab. It uses
[`bambulabs_api`](https://github.com/BambuTools/bambulabs_api) for local MQTT
telemetry and publishes one
[AC lab STATUS_SPEC v1.2](../ac-organic-lab/docs/STATUS_SPEC.md) surface per
printer. This repo conforms to lab status spec v1.2 on its per-printer
surfaces; the aggregate gateway envelope stays on v1.0 (it fronts printers and
has no primary operation of its own).

It runs a **submission pipeline**: remote users upload a print artifact (or an
`.stl` the gateway slices with Bambu Studio), the gateway validates it against
the target machine's profile, and valid jobs wait in a per-machine queue with
expected finish times. That path writes to the gateway's own disk and never to
a printer.

By default the service is **monitoring-only** and exposes no control endpoints.
A deployment that sets `dispatch.enabled` gets a narrow control plane: claims
plus five verbs (camera snapshot, chamber light, start an approved job, stop,
and — on the Bambu A1 only — moving the bed to a named position),
behind the gates described in [Control plane](#control-plane). A person
confirms the plate is empty before every start, except on a printer whose
**agent mode** an admin has switched on — approved 2026-11-02 for the Bambu A1
(`bambu_a1_01`) only — where an agent may decide from a claimed camera
snapshot ([Agent mode](#agent-mode-bambu-a1-only)). The third-party library's
other command methods remain unreachable. Control is not yet exposed as merged
`lab-skills` skills (ac-organic-lab PR #98 adds the A1's); until then the lab
rules let agents use the A1 agent-mode workflow on this API directly.

Every job keeps a durable, schema-versioned **result bundle** and its exact
artifact on the gateway's disk. Filing a record in the ELN is optional and goes
only through the central dashboard's ingest ([Print records and ELN
ingest](#print-records-and-eln-ingest)); the gateway never writes to the ELN.

## Documentation for agents

Read the [operating guide](src/bambu_server/docs/AGENT_GUIDE.md) for identity,
A1 agent mode, slicing/submission, claimed-camera decisions, external-spool
mapping, completion and recovery. The [API overview](docs/API_REFERENCE.md)
summarizes the endpoint families.

The service serves `/agent-docs`, `/agent-docs/api-reference` (generated from
its actual OpenAPI), `/llms.txt`, and the JSON `/docs/agent` guide. All are
credential-free documentation reads without printer I/O. Swagger at `/docs`
is read-only. Dashboard registration belongs on the shared `bambu_gateway`
entry; deploy these routes before enabling the new registry links.

## Architecture

```text
Bambu printers -- local MQTT/TLS --> background monitors --> cached status
                                                              |
Lab dashboard ---------------- HTTP GET /printers/{id}/status -+
                                                              |
Remote user -- POST /submissions --> (.stl: Bambu Studio CLI slice)
                                     validate against the ------+  (reads the cache)
                                     machine profile
                                              |
                                              v
                                     per-machine queue --> GET /printers/{id}/queue
                                              |  person approves
                                              v
Person on /ui -- claim, snapshot, confirm plate, start_print   (dispatch.enabled only)
                                              |
                                              v
                                     FTPS upload (size-verified) + MQTT start
```

Status, profile and queue requests only read the cache. They never connect to a
printer or request a telemetry refresh. The background monitor starts only the
MQTT client. The camera and FTP connections are opened only by the claimed
control routes, one transfer or one frame at a time.

## Install

Requires Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
cp printers.example.yaml printers.local.yaml
cp .env.example .env
```

Edit the two local files. To find a Bambu printer's LAN access code, enable LAN
mode and use the printer's network settings. The printer and service host must
be able to reach each other on the local network.

`printers.local.yaml`, `.env`, printer addresses, access codes, and serial
numbers are intentionally gitignored. Each printer entry names an environment
prefix; the service resolves `<PREFIX>_HOST`, `<PREFIX>_ACCESS_CODE`, and
`<PREFIX>_SERIAL` at startup.

## Run

```bash
uv run bambu-server
```

Defaults: `127.0.0.1:8012`, overridden by `BAMBU_SERVER_HOST` and
`BAMBU_SERVER_PORT`. Keep the service on the lab LAN/Tailnet; it has no
application-level authentication because access is expected to be gated by
Tailscale ACLs.

## HTTP surface

Gateway routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Service identity and configured printer count |
| GET | `/health` | Process liveness |
| GET | `/printers` | Safe printer inventory (no addresses or credentials) |
| GET | `/status` | Aggregate gateway envelope (one component per printer) |
| GET | `/ui` | Submission page for people (see below) |
| GET | `/whoami` | Whether this request carries an edge-verified identity |

Per-printer STATUS_SPEC routes:

| Method | Path |
|---|---|
| GET | `/printers/{id}/` |
| GET | `/printers/{id}/health` |
| GET | `/printers/{id}/status` |
| GET | `/openapi.json` |

Submission pipeline routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/printers/{id}/profile` | The machine a submitter targets and is validated against |
| GET | `/printers/{id}/queue` | Running job and waiting submissions, with finish times |
| POST | `/submissions` | Upload a `.3mf` / `.gcode` artifact (optional `eln_project`); validated inline |
| GET | `/submissions` | List jobs: `machine`, `state`, `limit`, plus `updated_after`, `order=newest`, `offset` |
| GET | `/submissions/{submission_id}` | One job with its verdict and history |
| POST | `/submissions/{submission_id}/approve` | Record sign-off on a queued job (optional `eln_project`) |
| POST | `/submissions/{submission_id}/cancel` | Withdraw a waiting job from the queue (keeps the artifact) |
| DELETE | `/submissions/{submission_id}` | Delete a finished job's record and artifact; its result bundle stays |
| GET | `/submissions/{submission_id}/artifact` | The exact stored file; `ETag` is its quoted SHA-256, `If-None-Match` answers 304 |
| GET | `/submissions/{submission_id}/result` | The schema-versioned result bundle (`final` once terminal) |
| POST/GET | `/submissions/{submission_id}/eln-ingest` | Queue a terminal job's record for ELN filing via central / read its outbox entry |
| GET | `/results/outbox` | Outbox counts and entries |

Agent workflow and agent mode routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/submissions/{submission_id}/objects` | Per-object positions in bed coordinates (mm), plate index |
| GET | `/submissions/{submission_id}/pickup-targets` | Finished job's objects in bed mm, bed temperature and cooled readiness |
| GET | `/events` | Lifecycle events; ids persist across restarts (`after=` paging) |
| GET/POST | `/printers/{id}/agent-mode` | Read the agent mode switch and audit / flip it (verified admin only) |
| POST | `/printers/{id}/bed-check` | Advisory empty-bed comparison of a claimed snapshot (opt-in) |

With `dispatch.enabled: false`, no `/control/*` routes exist and no route
dispatches a print.

## Agent workflow

Object positions are reported in the **printer's bed frame**, never a robot's:
millimetres, origin at the front-left corner of the printable area, +X to the
right and +Y toward the back seen from the front of the printer, Z up from the
plate, exactly as the sliced toolpath addresses the bed. Compare them with
`bed_size_mm` from `GET /printers/{id}/profile` (also in the result bundle).
The agent driving the arm owns the bed-to-arm calibration and transform; the
gateway stores none and never contacts or moves a robot.

Positions come from the sliced file, best source first:

1. `slicer_plate_json` — Bambu Studio's own per-object bounding boxes for the
   sliced plate (`Metadata/plate_N.json`, `bbox_objects`);
2. `gcode_label` — the extruding moves inside the slicer's object labels
   (`; start printing object, unique label id: N`, `EXCLUDE_OBJECT_START`, …),
   travel and purge moves excluded;
3. `model_transform` — the 3MF build item transform, used for plate 1 only
   because later plates are offset in the project layout.

Each object carries `object_id`, `name`, `center_x_mm`/`center_y_mm`,
`bounds` (min/max XY), `rotation_deg` where the source has it, `plate_index`
and `source`, plus `gcode_bounds` — the labelled extrusion footprint — as an
independent cross-check. A file that does not say where its objects are yields
an empty list, never a guess. For a multi-plate 3MF only the inspected (first
sliced) plate is reported.

**Events.** `GET /events?after=<event_id>` (or the webhooks in
`agent_workflow.event_webhook_urls`) delivers `job.started`, one event per
terminal state (`job.finished`, `job.failed`, `job.rejected`,
`job.cancelled`), `job.cooled`, `bed.moved` (A1 `position_bed`), and
`gateway.restarted`. Each event carries
`schema_version`, `event_id`, `gateway_id`, `boot_id`, `submission_id`,
`printer_id`, `state`, `updated_at`, `eln_project` and `note`. Event ids are
zero-padded decimals that keep increasing across restarts (they are kept in
`events.jsonl` beside the submissions), and every restart emits a
`gateway.restarted` marker with a new `boot_id`; a failure the gateway records
at startup because a restart interrupted a dispatch is a `job.failed` with
`recovered_after_restart: true`.

`job.finished` includes the job's `object_positions` (bed mm) and `pickup`:
the cached bed temperature, `agent_workflow.pickup_safe_bed_c`, and `cooled`.
When the bed is still hot, the gateway keeps watching its cached temperature
(no printer I/O) and emits `job.cooled` once it is safe, for up to
`agent_workflow.cooldown_watch_timeout_s` (the watch is not resumed after a
restart; poll `pickup-targets` instead). Emission and webhook failures are
logged and never affect a job.

**End-to-end for an agent:**

1. `POST /submissions` (sliced `.3mf`, or an `.stl` with `material` and
   optionally `placement=front_left`; `target_machine`; optional `eln_project`).
2. Approval: a person approves — or, on the A1 in agent mode, the agent.
3. `POST /printers/{id}/control/claim`, then keep it alive with `heartbeat`.
4. On the A1, `POST /printers/bambu_a1_01/control/position_bed`
   `{"position": "inspect", "bed_path_clear": true}` once the arm is confirmed
   clear, because the last print left the bed parked forward out of the
   camera's view.
5. Plate check: a person confirms (`in_person` / `printer_camera`) — or, in
   agent mode, the agent takes `POST /printers/{id}/control/snapshot`, judges
   the returned `image_base64` itself, and keeps the `token`.
6. `POST /printers/{id}/control/start_print` (see [Agent mode](#agent-mode-bambu-a1-only)),
   then release the claim.
7. Wait for `job.finished` (or `job.failed`) on `/events` or a webhook. The
   A1's end gcode parks the bed at `end_bed_y_mm` (Y 250 here) for pickup.
8. Wait for cool-down: `job.cooled`, or poll
   `GET /submissions/{id}/pickup-targets` until `ready`.
9. Hand the bed-frame `objects` and `bed_park_y_mm` to the arm agent, which
   applies its own calibration. The bundle at `/submissions/{id}/result` keeps
   the record.

`agent_workflow.vision_enabled` adds an optional, dependency-light advisory
check, `POST /printers/{id}/bed-check`, that compares a claimed snapshot with an
operator-captured `<printer_id>.jpg` empty-bed reference. It returns `clear`
only on an exact match and otherwise `occupied` or `unknown`. Its result is not
a plate check; an agent in agent mode may use it as one input to its own
decision.

### Agent mode (Bambu A1 only)

Approved by Yang on 2026-11-02. With the switch on, an agent may submit,
approve and start jobs on the Bambu A1, deciding itself that the plate is empty,
and (approved 2026-11-05) move its bed with `position_bed`.
Everywhere else, and on the A1 with the switch off, agents are refused and the
human-in-the-loop path is unchanged.

- **Eligibility** is static config: `agent_mode_eligible: true` on the printer.
  The config refuses it on any model but a Bambu A1 (not the A1 mini).
- **The switch** is off by default and is runtime state. Only an edge-verified
  identity whose role is in `dispatch.agent_mode_admin_roles` (default
  `admin`) can flip it, with `POST /printers/{id}/agent-mode
  {"enabled": true|false, "reason": "..."}`; an agent cannot, even as an
  admin. Every flip is appended to `agent_mode_audit.jsonl` (who, role, when,
  reason, previous value) before it takes effect. `GET .../agent-mode` returns
  the state and recent audit; `details.agent_mode` on `/printers/{id}/status`
  and on `/status` shows it too. An unreadable switch file fails closed (off).
  Turning it off refuses the next agent action at once; a running job is not
  affected.
- **Who is an agent:** a request with an `X-Agent-Id` header, or whose
  edge-verified role is in `dispatch.agent_roles` (default `agent`). The
  verified identity and the agent id are both recorded
  (`requested_by_agent`, `approved_by_agent`, `dispatch.dispatched_by_agent`).
- **Refusals** are 403 with `agent_mode: "not_eligible"` (any printer but the
  A1) or `agent_mode: "off"` (switch off), for agent submissions, approvals,
  bed moves and starts.

An agent's start, on the A1 with the switch on:

```json
POST /printers/bambu_a1_01/control/start_print
X-Claim-Token: <claim token>
X-Agent-Id: <agent id>

{"submission_id": "<id>", "plate_confirmed_empty": true,
 "plate_check_method": "agent_camera", "snapshot_token": "<from /control/snapshot>",
 "plate_check_rationale": "optional: why the plate looks empty",
 "ams_mapping": [{"filament_id": 1, "ams_id": 0, "tray_id": 1}]}
```

`agent_camera` needs a `snapshot_token` taken from that printer under the same
claim and no older than `dispatch.agent_snapshot_max_age_s` (120 s by
default); the gateway never captures a frame on the agent's behalf. An agent
must use `agent_camera`, and only an agent may. The record keeps the deciding
agent, its identity, the rationale and the frame's SHA-256; the frame is
archived as `GET /submissions/{id}/plate.jpg`. Every other gate still applies:
the claim, a cold idle printer, the declared plate, revalidation, the tray
mapping, verified identities, and one attempt per job with no retry.

**H2D camera:** snapshots are not supported for the H2D yet — its camera is not
the P1/A1 port-6000 service this gateway reads. `control/snapshot` answers 501
and the viewer 503 without opening a connection, so an H2D plate check is in
person only. The H2D is not eligible for agent mode in any case.

## Print records and ELN ingest

Records are durable by default. Terminal jobs and their artifacts are not swept
(`submissions.retain_terminal_days` is null unless set), and cancelling keeps
the artifact (`submissions.cancel_deletes_artifact: true` restores deletion).

When a job reaches a terminal state the gateway writes one result bundle to
`<submissions>/results/<submission_id>.json` (temp file, then `os.replace`),
and never prunes it — not even on `DELETE`. Schema `ac.bambu.print_result`,
`schema_version` 1: gateway id and version, printer id, state, `eln_project`,
the artifact (kind, filename, size, SHA-256, still stored, download path), a
timeline (submitted, approved, dispatch requested, command published, observed
running and finished, terminal), the plate check (method, who decided, agent
id, rationale, snapshot SHA-256), `bed_size_mm`, `objects` in bed mm, `pickup`
readiness at finish, and the full job record with history, verdict and facts.
`GET /submissions/{id}/result` returns it; for a job still in play it returns
one built on request with `final: false`. Terminal jobs from before this
existed get a bundle at the next startup, without an event.

**ELN ingest is optional** and follows the lab's OT-2 gateway pattern: devices
never write to the ELN; they push only to the central server's `/api/ingest/*`,
which alone holds ELN credentials. Each terminal job gets an outbox entry in
`<submissions>/outbox/<submission_id>.json`:

- `local_only` — no ELN project chosen, or no central ingest configured
  (`local_reason` says which);
- `pending` → `delivered` → `filing` → `filed`, or `held` (the central server
  refused it or held it for a person).

A job is queued automatically when it has an `eln_project` (from submission or
approval) and ingest is configured. To file one later,
`POST /submissions/{id}/eln-ingest {"eln_project": "..."}` on a terminal job
(refused with 409 while ingest is not configured, or once it is already
queued or filed; a `held` or `local_only` entry can be re-queued). The worker
POSTs `{schema_version, device_id, gateway_id, id, eln_project, bundle}` to the
ingest URL with `Authorization: Bearer $BAMBU_RESULTS_TOKEN`, then polls
`GET {url}/{device_id}/{submission_id}` for `{"state": "filing" | "filed" |
"held"}`; `device_id` is the printer id. Network errors, 408/425/429 and 5xx
retry with exponential backoff (`results.retry_initial_s` doubling to
`results.retry_max_s`); other 4xx hold the entry; a 404 on the status poll
delivers again. Entries are files, so the outbox resumes after a restart.
`/status` reports `results_ingest_configured`, `results_outbox` (count per
state), `results_pending` (pending plus delivered), `results_filing` and
`results_held`. Outbox and bundle failures are logged and never affect a job.

Deployment env vars (see `.env.example`):

| Variable | Purpose |
|---|---|
| `BAMBU_RESULTS_URL` | Central ingest URL, e.g. `https://<dashboard>/api/ingest/print-results`; overrides `results.ingest_url` |
| `BAMBU_RESULTS_TOKEN` | Bearer token for that ingest; required, read from the environment only |
| `BAMBU_EDGE_SHARED_SECRET` | Needed for verified identities, hence for admin flips of agent mode |

The status envelope uses `equipment_kind: other` because the authoritative
contract does not yet define a `3d_printer` kind. `details.device_type` carries
`3d_printer` without extending the closed enum locally. A reachable printer
reporting `FAILED` maps to `error`; missing or stale MQTT telemetry maps to
`unknown`, never to a fabricated hardware fault.

### Primary operation and `activity` (spec §2.3)

The **primary operation** of a printer is running a print job. `activity` is
derived from the printer's observed gcode state alone — never from
`equipment_status`, which answers the independent question of whether the
printer is healthy:

| observed gcode state | `equipment_status` | `activity` |
|---|---|---|
| `IDLE`, `FINISH` | `ready` | `idle` |
| `PREPARE`, `RUNNING`, `PAUSE` | `busy` | `running` |
| `FAILED` | `error` | `idle` |
| anything else | `unknown` | `unknown` |
| MQTT down / no telemetry / stale | `unknown` | `unknown` |

`PAUSE` counts as `running`: the job is in flight and the printer cannot accept
another one, which also satisfies the spec's `busy` ⇒ `running` invariant. The
exact sub-state stays visible in `components["print_job"]` and `message`.
`FAILED` is `idle` because the job has stopped — §2.3 permits any activity
under `error`.

### Rich read-only telemetry

Beyond the core state and metrics, a `data_ready` status enriches `details` with
read-only observations when the printer actually reports them (never as a bare
null or empty sentinel):

- `print_type` — source of the job (`cloud` / `local`).
- `nozzle_type`, `nozzle_diameter` — configured nozzle.
- `wifi_signal` — reported signal (dBm as a string).
- `print_error_code` — the printer's reported error code (`0` = none). A failed
  job's `last_error.message` appends the code when present.
- `skipped_objects` — object indices skipped in the current job.
- `ams_trays` — loaded AMS filament inventory: per-tray `tray_type`,
  `tray_color`, `tray_weight`, `tray_diameter`, `tray_temp`, and the spool's own
  `nozzle_temp_min` / `nozzle_temp_max` window, and `remaining_percent` (null
  when unreported). `ams_unit_ids` preserves units with no loaded filament.
  Unit/tray IDs are the inventory address; `tray_index` is retained as null
  for compatibility, because the MQTT `n` field is not a slot index.
  Tray/tag UUIDs are intentionally not surfaced (identifiers, not inventory).

AMS inventory is decoded from the MQTT client's cache without the library's
hub conversion: optional `ams_exist_bits`, `n`, and spool-tag fields must not
be required to recognize a loaded tray. Unknown inventory is distinct from a
known empty list. The submission page shows each reported tray and remaining
percentage, including AMS-HT units. Percentages are printer estimates; missing
estimates are never replaced with a guessed full spool.

Both status trays and profile trays include `tray_color_name` and
`tray_color_source`. Exact Bambu catalog color matches are display labels,
not proof of spool manufacturer. An all-zero color is unknown by default.
An operator can declare a color in a printer's local
`profile.ams.color_labels` list, using `ams_id`, `tray_id`, `material`,
`reported_color` (eight hex digits), and `name`. The declaration applies only
while that slot, material, and reported color match, and its source is
`operator_declared`. Review it after replacing a spool; identical telemetry
cannot identify a physical replacement. These local labels never influence
material validation or printer commands.

See [API reference](docs/API_REFERENCE.md) for field definitions, color source
values, authenticated browser URLs, and the submission/dispatch boundary.

The submission page supports `?embed=1`, inherits the same-origin dashboard's
light/dark theme, and accepts theme/selection messages only from that parent.
Standalone links should use the authenticated `/bambu/ui/` edge path, never
the gateway's unauthenticated port.

These are best-effort: a failure in any one getter is isolated, and the core
`activity`/state decision never depends on them.

`activity_since` is the instant the value last changed, observed by the
background poll (every `poll_interval_seconds`), not the time the status request
was built. It is `null` whenever the transition itself was never observed — a
service restart mid-print, or telemetry that went stale and recovered — because
the span began before this service could see it and stamping first-observation
time would report a far-too-short duration. Expect `activity: running` with
`activity_since: null` for a job that was already underway when the service
started; it gets a timestamp at the next real transition.

Print jobs run far longer than the dashboard's 60 s poll, so the sampling caveat
in §2.3.1 does not apply and no `cycles_total` metric is published.

## Submission pipeline

Remote users submit a print artifact to the gateway, an agent-style checker
validates it against the **specific machine** it is destined for, and valid jobs
wait in a per-machine queue with expected finish times. The design contract is
[`docs/SUBMISSION_PIPELINE_DESIGN.md`](docs/SUBMISSION_PIPELINE_DESIGN.md).

```text
submitted -> validating -> validated -> queued -> approved -> dispatching -> running -> finished
                       \-> rejected (terminal)        |             \-> failed  \-> failed
                                          \-----------\-> cancelled (terminal)
```

Everything up to and including approval is analysis and bookkeeping. Approval is
a *record*, not an action: it marks the job `approved` and sets
`verdict.dispatch_ready`, and moves nothing. Only the control plane
(`dispatch.enabled: true`, see *Control plane* below) moves a job past
`approved`, and only when a person starts it.

### STL slicing

With `slicer.enabled`, `POST /submissions` also takes an `.stl` for any printer
listed under `slicer.machine_profiles`, plus a configured `material`. Each
printer may name its own machine, process and filament presets; a bare machine
name retains the shared process/filament defaults. An explicit per-printer
filament map replaces those defaults. Presets with a `compatible_printers`
list must include the selected machine, including inherited restrictions.
The gateway runs the installed Bambu Studio CLI
headlessly in a scratch directory, forcing two machine facts the stock presets
get wrong: the nozzle type the printer reports and the build plate declared in
its profile. The resulting `.3mf` is then validated and queued like any other
upload, and the job carries `provenance` (source mesh hash, presets, slicer
version, placement). Slicing runs inline in the request.

By default the slicer centres the part. The optional form field
`placement=front_left` instead puts its footprint `slicer.placement_margin_mm`
(default 10 mm, room for a brim) in from the front-left corner of the printable
area, resting on the plate and keeping its orientation. The gateway wraps the
mesh in a one-object 3MF whose build transform carries the offset and slices
with `--arrange 0`, since the CLI drops a bare STL off the plate when arrange
is off. On the A1 this keeps the part clear of the purge line, which runs along
the front edge at X 108-158.

For A1 server-side PLA slicing, add the following alongside existing printer
entries under `slicer.machine_profiles` in the local configuration:

```yaml
bambu_a1_01:
  machine: Bambu Lab A1 0.4 nozzle
  process: 0.20mm Standard @BBL A1
  filaments:
    PLA: Generic PLA @BBL A1
  end_bed_y_mm: 250   # optional: park the bed forward for pickup
```

`end_bed_y_mm` rewrites the Y of the stock end gcode's final park move (the
A1's `G1 X-48 Y180 F3600`, just before the motors are released) so a finished
plate is presented further forward; on the A1 a larger Y moves the bed toward
the front. It must lie within the printable area, and a preset whose end gcode
has no such move is refused rather than guessed at. The value is recorded as
`provenance.end_bed_y_mm` and returned by `pickup-targets` as `bed_park_y_mm`,
because object positions move with the bed.

Agents upload multipart fields `file=design.stl`,
`target_machine=bambu_a1_01`, `material=PLA` and optionally
`placement=front_left` to `POST /submissions`
through the authenticated edge with their own API key and `X-Agent-Id`.
A1 agent mode must be on. There is no separate `/slice` endpoint. HTTP 201
returns a stored submission; inspect its validation and state before approval.
Slicing and submission do not claim, upload to, or start the physical printer.

### Control plane

Off unless `dispatch.enabled` is set. When on, each printer gets STATUS_SPEC
v1.1 claims and five verbs under `/printers/{id}/control/`:

| Route | What it does |
|---|---|
| `claim` / `heartbeat` / `release` | Cooperative claim (§5); every other control route needs `X-Claim-Token` or returns 423. |
| `snapshot` | One JPEG frame from the printer's chamber camera, for the plate check. Kept 5 minutes; the frame used to start a job is kept beside the job as `GET /submissions/{id}/plate.jpg`. |
| `light` | Switch the chamber light (`{"on": true}`), e.g. before a snapshot. No motion, no heat. |
| `start_print` | Upload an approved job's `.3mf` and start it. |
| `stop_print` | Stop the job in flight. |
| `position_bed` | Bambu A1 only (approved 2026-11-05): move the bed to `inspect` (camera sees the whole plate) or `pickup` (where the arm takes parts). |

`position_bed` takes `{"position": "inspect" | "pickup", "bed_path_clear": true}`
and moves the bed to the Y configured under the printer's `bed_positions`
(`inspect_y_mm`, `pickup_y_mm`; keep `pickup_y_mm` equal to the slicer's
`end_bed_y_mm`). It sends exactly `G90` / `G1 Y<y> F<feed>` -- no X, no Z, no
homing -- and is refused (412) unless the printer is idle, error-free and not
running a gateway job; it needs an edge-verified caller, and an agent also needs
agent mode on. Each move emits a `bed.moved` event. The printer does not report
bed position, so confirm it with a snapshot; a refused or undelivered move
(502) is never retried. Config accepts `bed_positions` on an A1 only.

An operator who knows the arm stays clear of the bed can record that once
instead of confirming it before every move:

```yaml
bed_positions:
  inspect_y_mm: 128
  pickup_y_mm: 250
  arm_clear:
    declared_by: yang
    declared_on: 2026-11-05
    statement: >-
      The robot arm is always clear of the A1's bed travel; it reaches over
      the plate only while its agent holds the A1's claim.
```

`GET /printers/{id}/profile` publishes `bed_positions`, declaration included,
so agents can rely on it for `bed_path_clear` (`AGENTIC_LAB_DESIGN.md` §1.3).
Delete `arm_clear` to withdraw it; agents then confirm per move again.

`start_print` is refused (412, a body naming the failed gate) unless the printer
is observable, idle, not in a failed state and cold (`safe_bed_c`,
`safe_nozzle_c`); a build plate is declared and matches the plate the job was
sliced for; the job revalidates against the printer's current state; every
filament is mapped to a loaded tray holding that material (an AMS tray, or the
external spool as `ams_id` 254 / `tray_id` 0 for a single-filament job on a
printer without an AMS); and the request says
`plate_confirmed_empty: true`, by camera or in person. A camera confirmation
records a frame under this claim: the gateway captures it when start is pressed
(or uses a `/control/snapshot` token sent with the request), and refuses with
502 if the camera delivers none. On the A1 in agent mode an agent states it
instead with `plate_check_method: agent_camera` and the token of the frame it
judged ([Agent mode](#agent-mode-bambu-a1-only)); a person cannot use that
method and an agent cannot use the others. With `require_verified_identity`
(the default) both the approver and the caller starting it must be
edge-verified identities (403 otherwise).
`allowed_actions` on `/status` is computed by the same gate, so the two never
disagree.

Dispatch is attempted once per job and never retried. The upload is verified by
size on the printer; the job becomes `running` only when the printer reports
it. If the printer never does, or the service restarts mid-dispatch, the job is
`failed` with `dispatch.uncertainty` saying what is not known. Look at the
printer before resubmitting.

### Machine profile

`GET /printers/{id}/profile` publishes what a submitter targets. It merges the
operator-declared profile from `printers.local.yaml` (bed size, enclosure, safe
temperature envelope, forbidden materials) with what the printer currently
reports (nozzle, loaded AMS trays). Where the two overlap the observed value
wins and `*_source` says so; the declared value is the fallback for machines
whose live field is blank — a dual-nozzle H2D reports no parsable nozzle type.

### What is checked

| check | fails when |
|---|---|
| `machine_compatible` | sliced for another printer, or a nozzle diameter/type the machine does not have |
| `material_allowed` | the filament is on the machine's forbidden list, or the request's declared material contradicts the sliced one |
| `material_filament_match` | any required material has no matching loaded AMS tray or external spool |
| `nozzle_temp_in_band` | the configured **or commanded** nozzle temperature is outside the machine's limit or the loaded filament's window |
| `bed_chamber_temp_in_band` | the bed temperature is out of band, or a heated chamber is requested on a machine without one |
| `build_fits_plate` | the model's XY footprint exceeds the declared plate |
| `gcode_sanity` | the toolpath contains a refused command (firmware update, EEPROM write, PID retune, cold-extrude override, …). One `M500` is accepted: Bambu's stock bed-mesh save directly after `G29` in the machine start gcode (approved 2026-10-20) |
| `params_present` | filament type, nozzle temperature or bed temperature is missing — or a `.3mf` carries no sliced plate, so no printer could run it |

One failing check rejects the submission, and rejection is terminal.

A check whose inputs do not exist reports **`not_applicable`**, with the reason,
and is never reported as a pass. Unknown AMS inventory is reported as "not
compared"; known empty inventory fails the material check. Declaring `limits` and
`bed_size_mm` in the profile is what turns those checks on — there are no
built-in defaults, because a guessed limit is a fabricated machine fact.

The gcode scan is explicitly a **heuristic**, and the passing detail says so. It
is not a proof of safety.

Material matching is not a dispatch mapping: the person starting the job
explicitly selects every filament slot, and the gateway rechecks slot material,
per-slot temperature windows, and current printer state at dispatch. Approval
alone does not establish those guarantees or start a printer.

### What is read from an artifact

A `.gcode` is scanned for its slicer config comments (settings, estimated time)
and its `G0`/`G1` motion (the real plate footprint, measured as an extent so
placement cannot change the answer). A `.3mf` is a zip: a *sliced plate file*
embeds `Metadata/plate_N.gcode`, which is scanned the same way, with
`project_settings.config` and `slice_info.config` filling in the rest. An
unsliced project file is reported as such rather than guessed at.

Reads are bounded. Above `submissions.scan_max_bytes` only the head and tail of
an artifact are read — enough for a config block at either end — and the plate
footprint is then withheld rather than computed from a partial scan. XML
carrying a document type declaration is refused outright, since that is the only
place an entity expansion can be declared.

### Queue and expected finish time

`GET /printers/{id}/queue` is gateway-computed and side-effect free. The running
job's remaining time comes from the printer's own telemetry; each queued job's
duration is the slicer's estimate embedded in its artifact. Anything the gateway
cannot compute is `null` and `estimates_complete` is `false` — an unknown
remaining time makes every downstream estimate unknown rather than wrong.

The running job is *not* correlated with a submission. This service never
dispatches, so a running print was started by some other route to the printer
(Bambu Studio, the handset, the cloud) and the gateway reports only what it
observes.

### The page

`GET /ui` serves a submission page: pick a machine (its plate size, nozzle,
chamber and limits are shown so you know what you are targeting), upload a
file, and read the per-check verdict. It also lists that machine's queue with
finish times, and offers Approve / Cancel. With the control plane on, an
approved job gets **Start…**: the page claims the printer, shows a fresh frame
from its camera, asks for a tray per filament and for the empty-plate
confirmation, then starts the job and reports what the printer says. A running
job gets **Stop print**. The page never fetches a camera frame on its own.

It is one static file with **no build step and no external resources** — no
CDN, no npm, no bundler — served from the same origin as the API it calls, so
it needs no CORS exemption and works on an isolated lab network. It holds no
state of its own and calls only the public endpoints below, so it can do
nothing the API would refuse. It offers Approve only on a `queued` job, which
is the same rule the server enforces: never advertise an action that would be
refused.

The page has no sign-in. The name you type is a label, not an identity — see
*Identity and approval* below.

### Submitting

The page is the easy path. Directly:

```bash
curl -sS -X POST http://127.0.0.1:8012/submissions \
  -F file=@plate.gcode.3mf \
  -F target_machine=bambu_x1c_01 \
  -F requested_by=alice \
  -F material=PLA
```

The response is the job, with its per-check verdict, at `queued` or `rejected`.
Validation runs inline (the file read happens on a worker thread), so the caller
sees the verdict immediately.

Uploads are stored under `submissions.directory` named from the submission's
UUID — never from the client's filename, which is reduced to a basename and kept
only as display metadata. No response ever contains a stored path. Jobs are
mirrored to one JSON file each, so a restart does not empty a machine's queue.

### Identity and approval

This service has no login of its own, so who did what depends on how it is
reached, and every job records which of the two it got:

- **Through the lab's Caddy edge** (`/bambu/*` — see *Behind the dashboard's
  login* below), the edge authenticates the person against `ac_auth` and injects
  `X-Auth-User`. The gateway believes that header **only** when the request also
  carries `X-Edge-Auth` matching `BAMBU_EDGE_SHARED_SECRET`, which a caller
  coming straight off the tailnet cannot produce. The signed-in account becomes
  the recorded actor, overriding anything the client supplied — a signed-in
  person must not be able to file work under someone else's name — and
  `requested_by_verified` / `approved_by_verified` are `true`.
- **Reached directly**, `requested_by` / `approved_by` / `cancelled_by` are
  **opaque labels, not identities**, exactly as the status surface is
  unauthenticated. The `*_verified` fields are `false`.

Two properties are deliberate. The gateway **fails closed**: with no
`BAMBU_EDGE_SHARED_SECRET` configured it trusts no injected identity at all,
rather than believing a header it cannot check. And the secret is compared in
constant time, because `==` on a secret leaks it a byte at a time.

`GET /whoami` reports what the current request carries, which is how the page
words itself honestly — it distinguishes "not signed in" from "this deployment
cannot tell who you are" (`identity_available`).

Approval is human-in-the-loop by design: nothing auto-approves, and a submission
that did not pass validation can never be approved. The one exception is the
A1 in agent mode, where an agent may approve; the job then records the agent
as `approved_by_agent`.

### Behind the dashboard's login

The page is served at `/ui` relative to wherever the service is reached, and it
derives its API base by stripping that trailing `/ui` from its own URL. One file
therefore serves both the direct deployment and a path prefix behind the lab's
single Caddy edge, with no server-side rewrite and no build-time config — the
same arrangement as the OT-2 operator SPA.

Fronting it that way is the **only** way it participates in SSO: a session
cookie cannot be shared with this gateway on its own address, because raw
`100.x` addresses cannot carry a `Domain` cookie and `*.ts.net` is on the Public
Suffix List, so browsers drop tailnet-wide cookies. One origin behind the edge
means one login (see `ac-organic-lab/docs/AUTH_DESIGN.md`).

**Deploying it: [`docs/EDGE_DEPLOY.md`](docs/EDGE_DEPLOY.md).**

The route lives in `ac-organic-lab/deploy/Caddyfile.single-edge` as `/bambu/*`,
gated by `forward_auth` and injecting the identity described above; the dashboard
frames `/bambu/ui/` under Utils → 3D Printers. Once that route is live the
service's bind can go back to loopback, closing the unauthenticated
`/submissions` path on the tailnet.

Note the page answers at both `/ui` and `/ui/`. Serving only one would make
Starlette redirect between them with a `Location` that drops the edge prefix,
landing the visitor on the dashboard.

### Retention

Job records and artifacts are the lab's print records, so nothing is swept by
default. Setting `submissions.retain_terminal_days` sweeps terminal records
(`rejected`, `finished`, `failed`, `cancelled`) older than that at startup. A
job that is **still in play is never swept**, however old — one stuck in
`validating` is a signal, not litter. The set of terminal states is derived
from the transition table rather than listed twice, so a state added there
cannot be missed here. Result bundles under `results/` are never swept.

`DELETE /submissions/{id}` removes one finished job's record and artifact
immediately; its result bundle stays. Only a terminal job can be deleted:
withdrawing one that is still waiting is `cancel`, which leaves a record of the
decision — deleting it would erase that along with the job.

Sweeping happens at startup rather than on a timer so the store's on-disk and
in-memory views stay identical. A record removed underneath a running process
lingers in memory until a restart, which is exactly the divergence that made
hand-cleanup necessary before this existed.

### Cancelling

`POST /submissions/{id}/cancel` withdraws a waiting job. It is a **queue
operation, not an abort**: it is legal only from `queued` and `approved`, it
reaches no printer, and it is deliberately refused for anything past the queue —
stopping a running print is a control-plane action that needs a claim, and this
surface has none.

Cancelling keeps the stored artifact by default, as part of the record; with
`submissions.cancel_deletes_artifact: true` it deletes it instead and marks
`artifact_removed`. The job record stays either way, with the actor and reason
in its history, so the withdrawal remains auditable. There is no undo — a withdrawn job is resubmitted, not revived. An
approved job that is cancelled has its `dispatch_ready` retracted.

## Dashboard registration

Add one entry per printer to `ac-organic-lab/equipment.yaml` after deploying the
gateway. Keep the actual host in the lab's local registry/configuration flow.

```yaml
- id: bambu_x1c_01
  name: Bambu X1 Carbon 01
  platform: fabrication
  kind: other
  adapter: http
  protocol: "1.0"
  base_url: http://127.0.0.1:8012
  status_path: /printers/bambu_x1c_01/status
```

This repository does not modify `ac-organic-lab`; registration is a separate,
reviewed change once deployment details are known.

## Test and lint

```bash
uv run pytest -q
uv run ruff check .
```

All tests use fake backends and make no hardware or network calls.

## Deployment

`deploy/bambu-server.service` provides a hardened systemd baseline for a Linux
lab host. Install the repository at `/opt/bambu-server`, create its uv
environment and local configuration, then install and enable the unit. It binds
loopback by default so a reverse proxy or same-host dashboard is the intended
client. The unit uses the FastAPI app factory so `cors_origins` from the local
YAML is applied before middleware is constructed.

The unit runs with `ProtectSystem=strict`, so `submissions.directory` must stay
inside `ReadWritePaths` — the shipped units cover the whole install root, which
the default `var/submissions` sits under. Moving the directory elsewhere means
widening `ReadWritePaths` to match.

## Dependency note

The initial integration targets `bambulabs_api` 2.6.x (`>=2.6.6,<3`). It uses
only `Printer.mqtt_start()`, `Printer.mqtt_stop()`, telemetry getters, and the
MQTT message callback. The version cap makes a future breaking major upgrade an
explicit review.

`python-multipart` backs the submission upload form; it is Starlette's multipart
parser and is a runtime dependency only because `POST /submissions` exists.
Artifact inspection adds no dependency: `.3mf` containers are read with the
standard library's `zipfile`, `json`, and `xml.etree`.
