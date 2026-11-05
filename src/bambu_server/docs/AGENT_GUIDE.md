# Bambu gateway: operating guide for agents

This gateway monitors multiple Bambu printers and, where enabled, provides
claimed camera snapshots, chamber-light control, print starts and print stops,
and on the Bambu A1 only, moving the bed between two named positions
(`position_bed`). Each printer's primary operation is printing a job; the
aggregate gateway has no physical operation of its own. It does not home, jog,
move any other axis, heat on demand, calibrate a robot or move a robot.
Monitoring never starts or stops a print.

[API reference]({{root}}agent-docs/api-reference) ·
[OpenAPI]({{root}}openapi.json) · [Read-only Swagger]({{root}}docs) ·
[Documentation index]({{root}}llms.txt)

The binding lab contract is `ac-organic-lab/docs/AGENTIC_LAB_DESIGN.md`, Part I;
the wire contract is `ac-organic-lab/docs/STATUS_SPEC.md`; printer-specific
approvals are in `bambu-server/AGENT_RULES.md`. Documentation is not permission
to execute hardware actions. Agents respect live claims and preconditions and
never bypass a refusal. Registration in the lab plan executor is a separate
approval; these endpoints do not add it.

**How printer I/O is routed.** `lab-skills` has no merged Bambu skill yet
(ac-organic-lab PR #98 adds `snapshot`, `position_bed` and `start_print` for
the A1). Until it is merged, the lab rules (`AGENTIC_LAB_DESIGN.md` §1.3,
*Bambu A1 agent mode* and *Bambu A1 bed positioning*, the latter added
2026-11-05) allow exactly one route: the A1 agent-mode workflow below,
including `position_bed`, called directly on this gateway's HTTP API through
the authenticated edge. No other printer may be driven by an agent, and
nothing beyond that workflow is allowed on the A1. When the Bambu skill lands
in `lab-skills`, this exemption ends and the same steps go through the SDK.

**The A1 agent loop at a glance.** Submit (optionally an `.stl` with
`placement=front_left`) → check the validation and object positions → approve
→ claim → `position_bed` to `inspect` → claimed snapshot → your plate decision
→ `start_print` → release → wait for `job.finished` (the print's own end gcode
parks the bed at the pickup position) → wait for `job.cooled` → hand
`pickup-targets` to the arm agent. Each step is described below.

## Find the printer and establish identity

Paths below are relative to the gateway. In a signed-in browser, the gateway
is mounted at `/bambu`. The dashboard's `/api/equipment/{id}/documentation/`
surface only reads registered documents; it cannot proxy status or control.
For the dashboard assistant's SDK documentation discovery, request
`bambu_gateway`: the JSON guide describes the whole service, not one printer.

1. `GET /printers` returns configured IDs and their status paths.
2. Read `/printers/{id}/status`, `/profile` and `/queue`. The gateway's bare
   `/status` is an aggregate of monitor connectivity, not permission to print.
3. `GET /whoami` must identify the intended account as verified before approval
   or starting. Use an approved automation API key through the authenticated
   edge, with `X-Agent-Id` on every agent request. An arbitrary client-supplied
   name is an unverified label, not authentication. Never use another person's
   session or manufacture edge identity headers.
4. Read `/printers/{id}/agent-mode`. Eligibility is static configuration;
   `enabled` is the audited admin switch. Only the approved Bambu A1 workflow
   (`bambu_a1_01`) may use agent submission, approval, bed positioning and
   autonomous plate checks.
   H2D and P1S keep their human-in-the-loop rules. An agent cannot flip the
   switch, even if its request carries an admin role.

The edge verifies credentials and supplies identity; the gateway verifies the
edge's shared secret. The shared secret is not an agent credential. Some edge
deployments report an automation account using the legacy `user` role, so an
agent must still declare `X-Agent-Id`. Never infer authorization from that role
alone. Printer credentials, addresses, serials and raw MQTT are never public API
data and must not be included in prompts, submissions or logs.

## Slice, submit and approve

Prepare a sliced `.3mf` for the actual printer, nozzle, declared plate and
loaded material. Upload it through `POST /submissions` with `target_machine`;
the endpoint also accepts `.gcode` for inspection. Dispatch requires a sliced
`.3mf`. STL intake is available only when the deployment has enabled slicing
and configured that target's presets; it also requires `material`. Do not
assume that enabled A1 agent mode implies that A1 STL slicing is configured.

For server-side A1 slicing where configured, send multipart fields `file`
(the STL), `target_machine=bambu_a1_01`, and `material=PLA` to
`POST /submissions`, using your own API key and `X-Agent-Id`. There is no
separate `/slice` route. The request runs Bambu Studio on the server and
returns the validated, stored sliced `.3mf` submission with source hash and
preset provenance. Allow time for slicing; it is synchronous with the upload
request. It does not send any printer command. The server selects the target's
presets, so callers cannot silently use P1S process settings for an A1.

To put the part near the front-left corner of the plate instead of the centre,
add `placement=front_left`. The part keeps its orientation and is moved so its
footprint starts `slicer.placement_margin_mm` (default 10 mm) from the front
and left edges of the printable area, in the bed frame (origin front-left, +X
right, +Y toward the back). A part too large to fit there is refused with 422.
`provenance.placement` records the choice and `GET /submissions/{id}/objects`
reports where the slicer actually put it. `placement` is accepted only with an
`.stl`; a sliced `.3mf` prints where its own slicer placed it.

An agent may instead slice locally with Bambu Studio and submit the result.
Use A1 machine, process and filament presets for an A1; P1S/X1C process settings
are not substitutes. Check the validation verdict returned by submission.
HTTP 201 acknowledges a stored job; its state can still be `rejected`.
The gcode scan is a heuristic, not a proof of safety; review unavailable checks.

`POST /submissions/{submission_id}/approve` records approval. Agents may approve
only while A1 agent mode is enabled. Approval neither selects a filament tray
nor starts the printer. Submission IDs are 32 lowercase hexadecimal characters.
Use `POST .../cancel` to withdraw a queued/approved job; cancellation is not a
stop command for a running print.

## Claim, inspect the plate and start once

1. Acquire `/printers/{id}/control/claim` (see *How printer I/O is routed*) and keep the claim
   alive with `heartbeat`. Send its `X-Claim-Token` on every control request.
   Never take over another session's claim; release yours on exit. Hold one
   claim for the whole sequence: a snapshot is valid only under the claim it
   was taken with.
2. Read the printer's status and `allowed_actions`. These are current device
   preconditions, not a complete job-specific authorization. A listed
   `start_print` does not mean that the build plate is clear.
3. On the A1, move the bed into the camera's view first (see *Bed
   positioning* below): a finished print leaves the bed parked forward for
   pickup, where the camera cannot see the front of the plate, and
   front-left parts sit exactly there. Send `position_bed` with
   `{"position": "inspect", "bed_path_clear": true, "reason": "..."}`, then
   wait a few seconds for the move to finish before the snapshot.
4. Take `POST /printers/{id}/control/snapshot` under that claim. Inspect the
   returned image itself and retain its token. If an object is present, the
   plate is obscured, or the image is unclear, record occupied/unknown,
   release the claim and stop. Do not send a start request to test a known
   occupied plate. Ask a person to inspect/remove the object, then obtain
   fresh evidence before any later attempt.
5. Only after a clear decision, call `/printers/{id}/control/start_print` with the
   approved submission, `plate_check_method: agent_camera`, that snapshot
   token, an affirmative empty-plate decision, its rationale, and an explicit
   mapping for every filament. The snapshot must be from the same printer and
   claim, normally no older than 120 seconds (deployment-configurable).
6. Inspect the returned job state. A successful HTTP response can carry a
   failed job if running could not be confirmed. Never automatically repeat
   a failed, timed-out, disconnected or uncertain dispatch. Preserve the
   evidence and ask a person to reconcile the printer first.
7. Release the claim. The print runs without it.

The gateway still requires a reachable, observable, idle, cold printer; a
declared matching build plate; successful artifact revalidation; suitable
loaded material; verified approver/caller where configured; and a live claim.
One dispatch attempt is allowed per job. It archives the camera frame and
SHA-256 with the deciding agent, identity and rationale.

`GET /printers/{id}/camera.jpg` is view-only: it reads the camera at most once
per printer every two seconds and shares the frame across viewers. It is off
on page load and is never plate-check evidence. H2D camera capture is not
supported by this gateway; its human plate check must be in person.
The optional `/bed-check` compares an existing claimed snapshot with an
operator reference. Its output is advisory and never substitutes for the
agent's own visual decision.

## Bed positioning (Bambu A1 only)

`POST /printers/bambu_a1_01/control/position_bed` moves the bed along Y only,
in absolute mode (`G90` / `G1 Y<y> F<feed>`), to one of two positions the
deployment configures. Approved by Yang on 2026-11-05 (bambu-server PR #12)
and allowed for agents by `AGENTIC_LAB_DESIGN.md` §1.3 (ac-organic-lab #103).

| Position | Use |
|---|---|
| `inspect` | The camera sees the whole plate. Use it before the plate-check snapshot. |
| `pickup` | Where the robot arm takes parts off. A finished print already parks the bed here through its end gcode, so call it only to return the bed after an `inspect`. |

Request body: `position` (`inspect` or `pickup`), `bed_path_clear` (must be
literally `true`) and an optional `reason`, which is recorded. You name a
position, never a coordinate. The response gives `position`, the configured
`y_mm`, `requested_at`, `moved_by`, `moved_by_verified`, `agent_id` and a
`note`.

- **Before you send it:** `bed_path_clear: true` states that the robot arm is
  clear of the bed's travel and nothing stands above the plate. Confirm it
  from one of: the operator's standing declaration on the printer profile
  (`GET /printers/bambu_a1_01/profile`, `bed_positions.arm_clear`; on this
  deployment Yang declared on 2026-11-05 that the arm is always clear of the
  bed), the arm's own reported state, or a person's statement for that
  specific move. Name the basis in `reason`, e.g. `"plate check; standing
  arm-clear declaration 2026-11-05"`. If `arm_clear` is `null` and you have
  neither of the others, do not move the bed. The A1 camera shows the bed,
  not the arm.
- **The standing declaration has one condition:** the arm may enter the bed's
  travel (to take parts off) only while its agent holds the A1's claim, so no
  bed move can start mid-pickup. An arm agent must claim `bambu_a1_01` before
  reaching over the plate and release it once the arm is clear again. If you
  see the claim held by someone else, do not move the bed.
- **Gates:** the claim (423); an idle printer reporting no error and no
  gateway job in flight (412, and `position_bed` is missing from
  `allowed_actions`); an edge-verified identity (403); for an agent, A1 agent
  mode on (403 with `agent_mode`). Any other printer answers 412: the verb is
  configured on the A1 only.
- **After it:** the printer does not report bed position. Confirm the move
  with a claimed snapshot before relying on it. On 2026-11-05 the A1 accepted
  the move straight after a finished print, without homing, and `inspect`
  showed the whole plate.
- **Failure:** a refusal or a 502 (command not delivered) means stop and ask a
  person. Never retry the move, never "fix" it by homing or any other command,
  and never use it as a recovery step.
- Each delivered move emits a `bed.moved` event naming the position, the Y,
  who moved it and the reason.

## External spool and color labels

An AMS assignment uses the observed `(ams_id, tray_id)` pair. For a
single-filament job on a printer without an AMS, the external spool is
`ams_id: 254`, `tray_id: 0`. Inspect `status.details.external_spool` or
`profile.observed.loaded_trays`; do not guess a tray from MQTT slot numbers.
Map the slicer's actual `filament_id`, not an assumed index.

Material comes from `tray_type`. A human correction such as **PLA · White**
can appear as `tray_color_name: White`, `tray_color_source: operator_declared`,
while `tray_color` still retains a different printer-reported code. This is
an explicit display-label correction, not a telemetry rewrite or proof of
spool brand/identity. The declaration is matched against slot, material and
reported color and can cease to apply after a spool change. Remaining material
is an estimate or unknown; approval does not prove sufficient quantity.

## Observe completion, cooling and records

Use the job record and `/events?after=<event_id>` for lifecycle changes:
`job.started`, `job.finished`, `job.failed`, `job.rejected`, `job.cancelled`,
`job.cooled`, `bed.moved` and `gateway.restarted`.
`job.started` is not completion; wait for `job.finished` or a terminal failure.
Event IDs persist across restarts. Save the event cursor; do not turn event
delivery into an automatic command retry.

For pickup, wait for `job.cooled` or read `/submissions/{id}/pickup-targets`.
Its `ready` requires a finished job, a cooled bed and reported object positions.
Temperature alone does not establish safe robot clearance, part release or
graspability. Objects are in bed millimetres, origin front-left, +X right,
+Y back, Z up. The arm agent owns its calibration and transform; the gateway
never commands the arm. Unknown positions are an empty list, never a guess.
On the A1 the bed itself moves in Y, so bed coordinates do not fix a world
position on their own: `bed_park_y_mm` says where the job's end gcode parked
the bed (`null` when unknown, e.g. the stock Y 180 end move). A larger value
puts the plate, and every object on it, that much further toward the front;
on this deployment the park is Y 250, 70 mm forward of stock. After the print
the bed motors are released, so the bed can be pushed. If anything moved the
bed since the print (an `inspect` move, a person), return it with
`position_bed` `pickup` and check it with a snapshot before the arm relies on
it. A front-left part is out of the camera's view at the pickup position.

`/submissions/{id}/artifact` returns the exact stored bytes and SHA-256 ETag.
`/result` returns the durable versioned bundle; `final: false` means the job is
still in progress. The final bundle survives deletion of the submission record.
The start evidence, when available, is at `/submissions/{id}/plate.jpg`.

ELN delivery is optional and goes through central ingest. Check
`results_ingest_configured` and the outbox state. `local_only` is not proof
of ELN filing; `delivered` and `filing` are not `filed`. Do not configure
credentials or claim that a record was filed merely to complete a workflow.

## Health, refusals and recovery

Status/profile/queue/result reads use cached or stored data, without printer
I/O. The view-only camera is the documented exception. An unreachable or stale
printer is `unknown`, not `error`; a reachable failed print reports `error`
with `last_error.code: print_failed`. `activity` separately reports `idle`,
`running` or `unknown`. Readiness does not prove a clear bed.

| Response | Meaning and response by the agent |
|---|---|
| 401 | Login or claim heartbeat was lost. Stop using that claim. |
| 403 | Identity/admin requirement or agent mode refused the action. Do not impersonate a person or flip the switch. |
| 404 | Unknown job/printer/resource, or a route disabled in this deployment. Inspect OpenAPI. |
| 409 | Claim/job state conflict or ineligible admin toggle. Respect the holder/state. |
| 412 | A dispatch or bed-move precondition failed; the body names the gate, and may include Retry-After. Do not change parameters to evade it. |
| 423 | Missing, stale or wrong claim token on a hard-enforced control action. |
| 422 | Invalid request or failed slicing; inspect the response. |
| 501 | Unsupported snapshot model, including H2D. |
| 502 / 503 | Camera/upstream failure, an undelivered bed move or printer command, or a service component unavailable. A failed camera check is not a clear plate; an undelivered move is never retried. |

Refusal envelopes can carry gate-specific fields alongside `detail`; FastAPI
request validation uses its standard structured `detail` list. OpenAPI names
the operation's declared responses. A precondition refusal is not a printer
fault and must not be treated as evidence that a physical action occurred.

Background monitoring reconnects independently of HTTP reads. Gateway shutdown
does not stop the physical printer. A restart loses active claims and can
record an interrupted dispatch as failed/uncertain; it never authorizes replay.
Cooldown event watching does not resume after restart, so poll pickup readiness.
Turning agent mode off blocks subsequent agent submissions, approvals, bed
moves and starts; it does not stop an already-running print. Operator
inspection resolves ambiguous outcomes; never home or move the bed as a
recovery step (`position_bed` is for the plate check and pickup only).
