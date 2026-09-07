# Bambu Printer Gateway — Control Plane Design (proposal, for review)

**Status:** DRAFT — no control code may ship until this design is approved.
**Companion:** `docs/SUBMISSION_PIPELINE_DESIGN.md` — the concrete submission →
validation → queue → dispatch flow. Everything in it up to the approval gate is
built; its dispatch step is the work this design gates, and
`submissions.dispatch()` is the named, tested stub waiting on it.
**Binding constraints:** `../ac-organic-lab/docs/AGENT_RULES.md` and
`../ac-organic-lab/docs/STATUS_SPEC.md` (authoritative). This design must never
weaken them. Repo-local rules: `AGENT_RULES.md` (this repo) and `docs/TODO.md`.

---

## 1. Why this needs a design, not just endpoints

The gateway is monitoring-only today (`mode: monitoring_only`, no `/control/*`
routes, camera/FTP clients never started). The third-party `bambulabs_api`
package exposes every command method, but the repo deliberately keeps them
unreachable from HTTP. Two independent reasons:

1. **Repro safety.** A printer is a fabrication device. Start/pause/stop,
   temperature changes, motion, homing, and calibration are real operations and
   the request must be gated end-to-end, not reachable in one HTTP hop.
2. **Cooperative contention.** Two workflow clients (an ELN agent, a user on the
   dashboard, a scheduler) could race. The lab contract solves this with
   **cooperative claims** (STATUS_SPEC §5) — and claims are only meaningful if
   every control endpoint honors them.

So the plan below is the *path from monitoring-only to control-capable*, laid
out so a human can review it before any code lands.

## 2. Layered safety (STATUS_SPEC §0 model)

The spec defines four interlock layers. A printer gateway sits at each:

| Layer | Owner | Bambu-specific concern |
|---|---|---|
| 1. Hardware limits | printer firmware | heat limits, door-open bed-temp cap, end stops, thermal runaway |
| 2. Device state machine | **this gateway** | refuse to start a second job while `running`; refuse any command while `unknown` |
| 3. Skill preconditions | `lab-skills` catalog | the SDK checks skill `requires_states` before dispatching |
| 4. Project plan interlocks | workflow / ELN plan | a run only starts after the plan, claims, and materials are in place |

The gateway is responsible for **layer 2** (state machine, claims, `allowed_actions`
agreement). It does **not** re-implement layer 1, and it must not pretend to.

## 3. Scope of control (what a printer can do, ranked by risk)

From `bambulabs_api` 2.6.6. Grouped by risk, so the phased rollout below can
start at the safe end.

**A. Observatory / config state (lowest risk)**
Set light on/off, set print-speed %, set part/aux/chamber fan speed, skip
objects (only affects the running job), downgrade firmware.

**B. In-flight job control (moderate)**
Pause, resume, stop the current job. No motion is initiated that wasn't already
scheduled; these only change the state of an existing print.

**C. New work (higher risk — the "real" control)**
Start a print from uploaded gcode/3MF, upload a file, set bed/nozzle
temperature, set the filament, home the printer, move Z, calibrate the printer,
load/unload an AMS spool, reboot.

**D. AMS / material management**
`load_filament_spool`, `unload_filament_spool`, `set_filament_printer`, per-tray
selection. Safe enough to automate **only** with the tray inventory from the
monitoring side, and never while a job is running.

Deliberately **excluded** until separately justified: `reboot`,
`downgrade_firmware`, `upgrade_firmware`, `manual_update`. These destabilize the
device and belong in a maintenance window, not a routine control surface.

## 4. Request path

```
client (SDK / ELN agent / dashboard action)
  └─ lab-skills  ── validates skill preconditions (layer 3), carries plan context
        └─ POST /control/<verb>  ── gateway checks claim + preconditions (layer 2)
              └─ BambuLabsBackend.<verb>()  ── one command, returns hardware ack
```

- A control request is **synchronous**: it must reach the printer, get an ack,
  and update the cached status so the next `/status` reflects the new state.
- The gateway's background monitor stays authoritative for `activity`; a control
  op may trigger a state refresh but must never *invent* a transition.
- **No control call is ever made from a GET handler.** Control only happens on
  `/control/*` routes, always claim-gated.

## 5. Claim protocol conformance (STATUS_SPEC §5)

The gateway today has no claim surface. Before any `/control/<verb>` exists, it
must implement the v1.1 claim protocol:

- `GET /status` publishes `details.claimed_by` (`ClaimedBy | null`) and a
  correctly-derived `allowed_actions` list.
- `POST /control/claim` → `409` if a claim is already held by another session
  (cooperative, not authenticated).
- `POST /control/heartbeat` → refreshes the claim TTL; `423` on token mismatch,
  `409` if the claim has expired/reaped.
- `POST /control/release` → releases the claim; `423` on token mismatch.
- Claim expiry is reaped by a background task; an expired claim reverts to
  `details.claimed_by: null` and returns its grants to `allowed_actions`.

`allowed_actions` is the **gateway's single source of truth** for "what would the
device honor right now". It must be computed from the same predicate that the
`/control/<verb>` handler uses (STATUS_SPEC §6.2), so the adapter/UI/SDK never
sees drift between the advertised list and a real refusal.

## 6. Preconditions and interlocks (STATUS_SPEC §6)

### 6.1 State-machine gates — hard refusals

Any `/control/<verb>` is refused (HTTP 412, structured body) unless **all** of:

1. The gateway is in a determinable state: the printer is `connected` and
   `data_ready` and telemetry is **not** stale, and `activity` is not `unknown`.
   *Never command a printer you cannot currently observe.*
2. The caller holds the claim (or, for observability-only verbs in §3A, the
   gateway may allow them claim-less — decide per-verb and mirror it in
   `allowed_actions`).
3. The specific verb's gates pass — see 6.2.

### 6.2 Per-verb gates

| Verb | Required state gate | Notes |
|---|---|---|
| `light` / `fan` / `print_speed` / `skip_object` | any determinable state | safe whitelist |
| `pause` / `resume` / `stop` | `activity == running` on pause/stop; `pause`→`resume` only from `running` | stop is always allowed when running, even on others' behalf if they hold the claim |
| `start_print` | `activity == idle`, bed+nozzle at safe temp, claim held, plan context supplied | never auto-start a second job |
| `set_temperature` | `activity == idle`, claim held | refuse if door open warning / out of band |
| `home` / `move_z` / `calibrate` | `activity == idle`, claim held, human approval | motion |
| `load` / `unload` / `set_filament` | `activity == idle`, claim held | crosses the AMS |

### 6.3 Failure semantics

- A **precondition refusal** returns `412` + a body distinguishable *by shape*
  (not by `message` text), with `Retry-After` when recovery is time-bounded.
  A 412 **never** mutates `last_error` (STATUS_SPEC §6.3) and must not latch
  `error`.
- A **hardware/command failure** (the printer accepted the command then faulted)
  is an execution error: record `last_error`, set `activity`/`equipment_status`
  as the printer actually reports, and do not hide it. This is the one path that
  may set `error`.

### 6.4 Clearing policy (learned from the OT-2 gateways)

The lab already learned the hard way that a `/control/*` failure can lint a
device to `equipment_status: error` and leave a human-only recovery path
(OT-2 `/control/reconcile`). The Bambu gateway must therefore:

- Keep the **reconcile/clear** affordance explicit and human-gated — a printer's
  `error` should not be silently drained by an automated retry.
- Document the exact command that clears a latched `error` (e.g. a documented
  `cancel`/`clear_error` action, or the AMS/state recovery), and **never** let an
  agent invoke it without a claim and explicit approval.
- A successful control op auto-clears a *stale* `last_error` (STATUS_SPEC §6.4),
  but never a *current* run-blocking fault.

## 7. Human-approval gating

Not every verb needs a person. Proposal (to be decided at review):

- **Auto (with plan context + claim):** light, fan speed, print speed, pause,
  resume, stop, skip object. These are low-orphaning and reversible via the plan.
- **Needs explicit approval:** start_print, set_temperature, home, move_z,
  calibrate, load/unload filament, upload file. One-shot approval ticket, expired
  after the operation.
- **Never automated:** reboot, firmware downgrade/upgrade.

The ELN agent (per its own rules) is told to go direct to equipment and fail
loudly — but it must still go through this surface and hold a claim; "fail
loudly" must not mean "bypass interlocks".

## 8. Phased rollout

| Phase | Ship when | Content |
|---|---|---|
| 0 | already | monitoring-only (current) |
| 1 | after review | claim protocol + `allowed_actions` + `details.claimed_by` — **no real commands** yet |
| 2 | phase 1 verified | §3A observability verbs (light/fan/speed/skip) |
| 3 | phase 2 verified | §3B in-flight control (pause/resume/stop) |
| 4 | phase 3 verified | §3C start/upload/temp/filament, gate-checked |
| 5 | phase 4 verified | §3D AMS material management, claim+approval only |
| excluded | — | reboot, firmware management |

Each phase keeps `/control/reconcile` obvious and human-gated, and each phase
ships with fake-backend tests (no hardware).

## 9. What changes (enumerate, not implement yet)

- `src/bambu_server/models.py` — add `ClaimedBy`, claim models, per-verb request
  bodies; add control routes to the OpenAPI surface.
- `src/bambu_server/main.py` — `/control/claim`, `/control/heartbeat`,
  `/control/release`, then `/control/<verb>`; wire `allowed_actions` to the
  precondition helper (STATUS_SPEC §6.2).
- `src/bambu_server/backend.py` — expose a narrow, approved subset of
  `bambulabs_api` command methods behind the existing adapter; keep camera/FTP
  clients off unless a phase explicitly needs them.
- `src/bambu_server/monitor.py` — publish `details.claimed_by`, derive
  `allowed_actions` from the same gate predicate, and reconcile `error` on
  control recovery without auto-draining.
- `tests/` — fake backends; assert 409/423/412 behaviors, `allowed_actions`
  agreement, and that no GET handler performs control.

## 10. Review checklist

- [ ] Claim protocol matches STATUS_SPEC §5 (409/423, TTL reaping).
- [ ] `allowed_actions` is derived from the *same* predicate as `/control/*`.
- [ ] Precondition refusals are `412` with shape-distinguishable bodies.
- [ ] No `GET` handler ever issues a control command.
- [ ] `error` latching + human reconcile path documented and test-covered.
- [ ] All phase-4+ verbs are plan-context + claim + approval gated.
- [ ] Reboot/firmware verbs are explicitly excluded from the surface.
