# AC Bambu Server

A monitoring-only FastAPI gateway for the Bambu Lab printers in the lab. It
uses [`bambulabs_api`](https://github.com/BambuTools/bambulabs_api) for local
MQTT telemetry and publishes one
[AC lab STATUS_SPEC v1.2](../ac-organic-lab/docs/STATUS_SPEC.md) surface per
printer. This repo conforms to lab status spec v1.2 on its per-printer
surfaces; the aggregate gateway envelope stays on v1.0 (it fronts printers and
has no primary operation of its own).

The service deliberately exposes **no control endpoints**. The third-party
package supports commands, but those methods are isolated behind a narrow
monitoring adapter and are not reachable from HTTP. Future control work must go
through `lab-skills`, claims, preconditions, audited plans, and the
human-approval rules in the lab contract.

It also runs a **submission pipeline**: remote users upload a print artifact,
the gateway validates it against the target machine's profile, and valid jobs
wait in a per-machine queue with expected finish times. That whole path is
read-and-analysis — it writes to the gateway's own disk and never to a printer.
The one printer-touching step, dispatch, is not implemented; see
[Submission pipeline](#submission-pipeline).

## Architecture

```text
Bambu printers -- local MQTT/TLS --> background monitors --> cached status
                                                              |
Lab dashboard ---------------- HTTP GET /printers/{id}/status -+
                                                              |
Remote user -- POST /submissions --> validate against ------- +  (reads the cache)
                                     the machine profile
                                              |
                                              v
                                     per-machine queue --> GET /printers/{id}/queue
                                              |
                                              x  dispatch: not implemented
```

Dashboard requests only read the cache. They never connect to a printer or
request a telemetry refresh. The background monitor starts only the MQTT client;
camera and FTP clients are not started. The submission pipeline reads that same
cache and writes only to the gateway's own disk.

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
| POST | `/submissions` | Upload a `.3mf` / `.gcode` artifact; validated inline |
| GET | `/submissions` | List jobs (`machine`, `state`, `limit` filters) |
| GET | `/submissions/{submission_id}` | One job with its verdict and history |
| POST | `/submissions/{submission_id}/approve` | Record sign-off on a queued job |
| POST | `/submissions/{submission_id}/cancel` | Withdraw a waiting job from the queue |
| DELETE | `/submissions/{submission_id}` | Delete a finished job's record (retention) |

No `/control/*` routes exist, and no route dispatches a print.

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
submitted -> validating -> validated -> queued -> approved -> | dispatch
                       \-> rejected (terminal)                | not implemented
                                          \--------\-> cancelled (terminal)
```

Everything up to and including approval is analysis and bookkeeping. Approval is
a *record*, not an action: it marks the job `approved` and sets
`verdict.dispatch_ready`, and moves nothing. `dispatching`, `running` and
`finished` are declared by the contract but unreachable — no route in this
service can enter them.

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
| `material_filament_match` | any required material has no matching loaded AMS tray |
| `nozzle_temp_in_band` | the configured **or commanded** nozzle temperature is outside the machine's limit or the loaded filament's window |
| `bed_chamber_temp_in_band` | the bed temperature is out of band, or a heated chamber is requested on a machine without one |
| `build_fits_plate` | the model's XY footprint exceeds the declared plate |
| `gcode_sanity` | the toolpath contains a refused command (firmware update, EEPROM write, PID retune, cold-extrude override, …) |
| `params_present` | filament type, nozzle temperature or bed temperature is missing — or a `.3mf` carries no sliced plate, so no printer could run it |

One failing check rejects the submission, and rejection is terminal.

A check whose inputs do not exist reports **`not_applicable`**, with the reason,
and is never reported as a pass. Unknown AMS inventory is reported as "not
compared"; known empty inventory fails the material check. Declaring `limits` and
`bed_size_mm` in the profile is what turns those checks on — there are no
built-in defaults, because a guessed limit is a fabricated machine fact.

The gcode scan is explicitly a **heuristic**, and the passing detail says so. It
is not a proof of safety.

Material matching is not a dispatch mapping: per-filament slot selection,
per-slot temperature checks, sufficient quantity, and revalidation at dispatch
remain prerequisites for the future control plane. The current approval flag
does not establish those guarantees or enable printer execution.

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
finish times, and offers Approve / Cancel.

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
that did not pass validation can never be approved.

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

Terminal records (`rejected`, `finished`, `failed`, `cancelled`) are swept at
startup once older than `submissions.retain_terminal_days` (30 by default; set
it to null to keep everything). A job that is **still in play is never swept**,
however old — one stuck in `validating` is a signal, not litter. The set of
terminal states is derived from the transition table rather than listed twice,
so a state added there cannot be missed here.

`DELETE /submissions/{id}` removes one finished job's record and artifact
immediately. Only a terminal job can be deleted: withdrawing one that is still
waiting is `cancel`, which leaves a record of the decision — deleting it would
erase that along with the job.

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

Cancelling deletes the stored artifact (a withdrawn job has no further use for
it, and it is the submitter's data) and marks `artifact_removed`. The job record
stays, with the actor and reason in its history, so the withdrawal remains
auditable. There is no undo — a withdrawn job is resubmitted, not revived. An
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
