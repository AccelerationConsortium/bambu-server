# Bambu Printer Submission & Validation Pipeline — design spec (for implementation)

**Status:** IMPLEMENTED through dispatch. The dispatch step (§8) was approved by
a human on 2026-10-04 as human-in-the-loop with a mandatory empty-plate
confirmation, and ships behind `dispatch.enabled` (off by default). See
`README.md` → *Control plane* and `docs/TODO.md` for what shipped and what is
still owed. §8's notes about the FTP client being off and §11's "keep
Camera/FTP off" now read: opened only by claimed control routes, one transfer
or one frame at a time.
**Scope:** submission → model validation → per-machine queue → ETA → (approved)
dispatch. **This document is the contract the implementer builds against.**

---

## 1. Goal

Remote users submit a print design to the Lab gateway, an agent validates that
the model is safe and compatible with the **specific machine** it is destined
for, valid jobs wait in a per-machine queue, the dashboard shows the running job
and expected finish time, and only validated + approved jobs are dispatched.

This is read/analysis until the final dispatch step. The dispatch step is the
single printer-touching action and remains behind the lab approval gate.

## 2. End-to-end flow

```
user ── POST /submissions  (.3mf/.gcode + metadata: target_machine, material)
          │  file+metadata persisted; NO printer I/O
          ▼
[submitted] --agent validates the MODEL against the machine profile--
          │  pure analysis; NO printer I/O
          │  pass  -> [validated]
          │  fail  -> [rejected]  (report why; fail loud)
          ▼
[validated] --> [queued] per machine  (dashboard: running job + ETA)
          │
          ▼
[queued] --> [approved]  (human or agent sign-off, per the autonomy decision)
          │
          ▼
[approved] --> get claim --> dispatch (upload + start_print)  *** the gated step ***
          │
          ▼
[running] / [finished] / [failed]     (monitor reflects printer state; never invented)
```

A job is *control-plane* only at the `approved → dispatch` boundary. Everything
above it (submit, validate, queue, ETA, approve) touches no printer.

## 3. Machine profile — the thing users target

A remote user must be able to pick the exact machine they'll use, without being
near it. Publish one immutable profile per printer. Source of truth is the
gateway's own configuration (`printers.local.yaml` + the resolver), exposed
through a read endpoint; the dashboard/ELN can render it.

Fields (values a checker can validate against):

```yaml
machine:
  id: bambu_p1s_01          # stable id
  name: Bambu P1S 01
  model: P1S
  enclosure: "enclosed"     # P1S/H2D differ from open-frame
  nozzle_type: hardened_steel
  nozzle_diameter_mm: 0.4
  bed_size_mm: [256, 256]   # X, Y
  chamber_temperature_c: null   # H2D has chamber; P1S does not
  ams:                       # optional, if trays load
    filament_forbidden: []   # materials this machine won't run
```

`nozzle_type`/`nozzle_diameter` come live from the printer; the rest is
operator-declared on the machine record. The profile is the *checker's* target.

## 4. Submission API (no printer I/O)

```http
POST /submissions                # multipart: file + fields
  multipart:
    file: <model.3mf|model.gcode>
    target_machine: bambu_p1s_01
    material: PLA                # optional; inferred if absent
    requested_by: <user/owner id>   # from ac_auth identity if wired, else opaque id

201 -> submission  (job created in [submitted])
```

The gateway persists the file (hashed name by submission id; derive the
submission id from a UUID, never from the user path) plus the metadata. If
identity is wired, verify it here (`ac_auth`); if not, record the opaque owner
string. **This handler performs no printer I/O and no MQTT.**

- `400` malformed file / unknown `target_machine`
- `404` unknown target machine
- `413` file too large

Storage is on the gateway host, path in a gitignored directory. Never store the
file under a path derivable from a hostile filename.

## 5. Job state machine

```
submitted -> validating -> validated -> queued -> approved -> dispatching -> running
                              \-> rejected (terminal)
   any state except rejected -> failed (dispatch/run error)  -> human reconcile
```

States the agent owns: `validating`, `validated`, `rejected`. States the queue
owns: `queued`, `approved`. States the monitor owns: `running`, `finished`,
`failed`. `failed` follows the lab's latching rule (a run error may set
`equipment_status: error`; clearing is human-gated, never auto-drained).

## 6. Agent validation — "check the model before running it"

Run synchronously on submission, or as an async worker; either way it is
**pure analysis** of the uploaded artifact against the target machine profile.
It must report pass/fail with a structured, per-check verdict, and never degrade
to a silent pass. Check list:

| # | Check | Source | Fail condition |
|---|---|---|---|
| 1 | Machine exists & compatible | machine profile | target `id` unknown; nozzle diam/type mismatch |
| 2 | Material allowed | profile `ams.filament_forbidden` + tray data | material not runnable on this machine |
| 3 | Filament actually loaded | `ams_trays[].tray_type` | model material ≠ a loaded tray (when AMS data present) |
| 4 | Nozzle temp in band | tray `nozzle_temp_min/max` | model nozzle temp outside the loaded filament's range |
| 5 | Bed/chamber temp in band | machine profile + printer limits | out of the machine's safe range |
| 6 | Build fits plate | `bed_size_mm` vs model bounds | model exceeds bed size |
| 7 | G-code sanity (`.gcode`) | scan for M104/M140/etc. | disallowed/harmful sequences; junk |
| 8 | Params present | model settings | missing required print settings |

**Verdict output** (this is the machine-readable result the queue consumes):

```json
{
  "submission_id": "…",
  "verdict": "pass|reject",
  "checks": [
    {"check": "material_filament_match", "ok": true, "detail": "PLA loaded tray 1"},
    {"check": "nozzle_temp_in_band", "ok": false, "detail": "220C > tray max 210C"}
  ],
  "reasons": ["nozzle_temp_in_band"],
  "dispatch_ready": false
}
```

Rules: a single failing check ⇒ `reject`; `dispatch_ready` is true only on a
full pass **and** any approval gate. For `.gcode` the checker explicitly notes
it is doing a heuristic scan, not a formal safety proof — don't overclaim.

### Where the checker runs

A `lab_skills` skill (e.g. `bambu.validate_model`) is the canonical home — that
keeps control routes free of validation logic and matches the lab's
skill-dispatch model (layer 3 preconditions). It takes the artifact path + the
machine profile and returns the verdict. No command methods are called here.

## 7. Queue + expected end time (the read surface)

Per-machine FIFO queue of `validated`/`approved` jobs. ETA is gateway-computed,
never guessed by an agent:

- For the **running job**: the printer already reports `remaining_time` (min); ETA
  = `now + remaining_time`.
- For each **queued job**: estimate duration (from the model's print time, or the
  slicer-reported time embedded in the `.3mf`/`.gcode`), then a job's ETA =
  now + Σ(earlier queued durations) + (current printer remaining).

The dashboard reads a read-only endpoint (or an extension of the per-printer
`details`) for the queue; **GET never triggers printer I/O or a queue mutation**.

```http
GET /printers/{id}/queue
-> { "running": {job_name, progress_percent, remaining_time, expected_end},
     "queued": [{job_name, estimated_duration_min, expected_end}, ...] }
```

## 8. Dispatch (the only gated, control-plane step)

Cascades exactly one transition per job, always under a claim
(STATUS_SPEC §5: `/control/claim` → heartbeat → release), after checking the
state-machine preconditions (§6 of the contract) and the approval decision:

- **Preconditions:** `activity == idle`, telemetry not stale, claim held, machine
  matches the job's target, bed/nozzle at safe temp.
- **Refusals:** `412` structured body when a precondition fails; `423` on claim
  token mismatch; `409` on a concurrent claim.
- **Operations** (phase-gated, via the existing adapter — never raw
  `bambulabs_api` methods from HTTP):
  - upload the gcode/3mf to the printer (starts the FTP client — currently
    deliberately not started, so this is a phase-4 item to enable),
  - `start_print` / `gcode_file`.

**Approval decision (must be resolved before dispatch is built):**
- *Autonomous*: the agent's `dispatch_ready` **is** the approval.
- *Human-in-the-loop* (recommended start): agent validates + recommends; a person
  clicks Approve → dispatch. Relax per machine once trust is established.

## 9. Permissions & repository rules

- **Read/analysis surface** (`/submissions`, per-machine profile, `/printers/{id}/queue`,
  agent validation): allowed with this design; no printer I/O.
- **Dispatch** is control-plane: needs the approved control design
  (`docs/CONTROL_PLANE_DESIGN.md`), claims, interlocks, and the chosen approval
  model. Do not expose `/control/*` in this work.
- Never return access codes, serials, IPs, or raw MQTT payloads from any
  endpoint. Submissions store files on the gateway host; never echo file paths
  back to clients.

## 10. Data gaps the implementer will hit

- **AMS parsing is implemented in the gateway.** Optional presence bits,
  calibration fields and incomplete spool tags no longer suppress inventory.
  Unknown inventory still makes checks non-blocking at intake; dispatch must
  require fresh, complete material mapping and validation under its claim.
- **`nozzle_type` is `None` on H2D** (library enum can't parse a dual-nozzle
  report). The profile should carry nozzle config explicitly so machine checks
  (#1) don't depend on a blank live field.

## 11. Out of scope (for this pipeline)

- Booking/auth/calendar gating — reverted by decision; the queue is cooperative.
- Enforcing against Bambu Studio/Cloud bypass — advisory only.
- Camera/FTP clients (except the one upload call dispatch needs) — keep off
  unless a phase explicitly requires them.
- Rebooting, firmware management, motion/temperature set — excluded verbs.

## 12. Open decisions (blockers to confirm before implementation)

1. Approval model: agent-autonomous vs human-in-the-loop for dispatch.
2. Identity: is submission tied to `ac_auth` now, or an opaque `requested_by`?
3. Queue source: gateway-owned (recommended, gives real ETA) — confirm.
4. `.3mf` parser choice for duration/material extraction.
5. Is the agent-authorized run permitted, or must a person always approve?

---

**Handoff note for the implementing agent:** build the submission intake,
the validation skill/checker (against the machine profile), the per-machine
queue + ETA read endpoint, and the job state machine. **Leave dispatch stubbed**
behind an approval hook; do not expose `/control/*`. Follow `AGENTS.md`:
`uv run ruff check .` and `uv run pytest -q` must pass; tests use fake backends
and never touch hardware.
