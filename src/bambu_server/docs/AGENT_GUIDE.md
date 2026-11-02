# Bambu gateway: operating guide for agents

This gateway monitors multiple Bambu printers and, where enabled, provides
claimed camera snapshots, chamber-light control, print starts and print stops.
Each printer's primary operation is printing a job; the aggregate gateway
has no physical operation of its own. It does not home, jog, heat on demand,
calibrate a robot or move a robot. Monitoring never starts or stops a print.

[API reference]({{root}}agent-docs/api-reference) ·
[OpenAPI]({{root}}openapi.json) · [Read-only Swagger]({{root}}docs) ·
[Documentation index]({{root}}llms.txt)

The binding lab contract is `ac-organic-lab/docs/AGENTIC_LAB_DESIGN.md`, Part I;
the wire contract is `ac-organic-lab/docs/STATUS_SPEC.md`; printer-specific
approvals are in `bambu-server/AGENT_RULES.md`. Documentation is not permission
to execute hardware actions. Agents route printer I/O through `lab-skills`,
respect live claims and preconditions, and never bypass a refusal. Registration
in the lab plan executor is a separate approval; these endpoints do not add it.

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
   (`bambu_a1_01`) may use agent submission, approval and autonomous plate checks.
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

1. Acquire `/printers/{id}/control/claim` through the SDK and keep the claim
   alive with `heartbeat`. Send its `X-Claim-Token` on every control request.
   Never take over another session's claim; release yours on exit.
2. Read the printer's status and `allowed_actions`. These are current device
   preconditions, not a complete job-specific authorization. A listed
   `start_print` does not mean that the build plate is clear.
3. Take `POST /printers/{id}/control/snapshot` under that claim. Inspect the
   returned image itself and retain its token. If an object is present, the
   plate is obscured, or the image is unclear, record occupied/unknown,
   release the claim and stop. Do not send a start request to test a known
   occupied plate. Ask a person to inspect/remove the object, then obtain
   fresh evidence before any later attempt.
4. Only after a clear decision, call `start_print` through the SDK with the
   approved submission, `plate_check_method: agent_camera`, that snapshot
   token, an affirmative empty-plate decision, its rationale, and an explicit
   mapping for every filament. The snapshot must be from the same printer and
   claim, normally no older than 120 seconds (deployment-configurable).
5. Inspect the returned job state. A successful HTTP response can carry a
   failed job if running could not be confirmed. Never automatically repeat
   a failed, timed-out, disconnected or uncertain dispatch. Preserve the
   evidence and ask a person to reconcile the printer first.

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

Use the job record and `/events?after=<event_id>` for lifecycle changes.
`job.started` is not completion; wait for `job.finished` or a terminal failure.
Event IDs persist across restarts. Save the event cursor; do not turn event
delivery into an automatic command retry.

For pickup, wait for `job.cooled` or read `/submissions/{id}/pickup-targets`.
Its `ready` requires a finished job, a cooled bed and reported object positions.
Temperature alone does not establish safe robot clearance, part release or
graspability. Objects are in bed millimetres, origin front-left, +X right,
+Y back, Z up. The arm agent owns its calibration and transform; the gateway
never commands the arm. Unknown positions are an empty list, never a guess.

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
| 412 | A dispatch precondition failed; the body names the gate, and may include Retry-After. Do not change parameters to evade it. |
| 423 | Missing, stale or wrong claim token on a hard-enforced control action. |
| 422 | Invalid request or failed slicing; inspect the response. |
| 501 | Unsupported snapshot model, including H2D. |
| 502 / 503 | Camera/upstream failure or a service component unavailable. A failed camera check is not a clear plate. |

Refusal envelopes can carry gate-specific fields alongside `detail`; FastAPI
request validation uses its standard structured `detail` list. OpenAPI names
the operation's declared responses. A precondition refusal is not a printer
fault and must not be treated as evidence that a physical action occurred.

Background monitoring reconnects independently of HTTP reads. Gateway shutdown
does not stop the physical printer. A restart loses active claims and can
record an interrupted dispatch as failed/uncertain; it never authorizes replay.
Cooldown event watching does not resume after restart, so poll pickup readiness.
Turning agent mode off blocks subsequent agent submissions, approvals and
starts; it does not stop an already-running print. Operator inspection resolves
ambiguous outcomes; never home or move the bed as a recovery step.
