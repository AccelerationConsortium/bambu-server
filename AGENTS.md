# AGENTS.md — shared agent instructions — bambu-server

Read this before proposing or editing anything in this repository. This file
adds repository-specific instructions to the canonical base in the sibling
`ac-organic-lab/AGENTS.md` repository.

## Binding contract

The following documents take precedence over this file and must not be weakened:

- `../ac-organic-lab/docs/AGENT_RULES.md` — lab-wide operating and safety rules.
- `../ac-organic-lab/docs/STATUS_SPEC.md` — the authoritative equipment HTTP contract.
- `AGENT_RULES.md` — repository-specific additions to the lab rules.

## Repository purpose

This service is a multi-printer gateway. It translates Bambu Lab's local MQTT
telemetry into one STATUS_SPEC v1.0 HTTP surface per configured printer.

Monitoring is the default. A human approved (2026-10-04) a narrow,
human-in-the-loop control plane, enabled per deployment by `dispatch.enabled`:
STATUS_SPEC v1.1 claims plus exactly four verbs -- camera snapshot, chamber
light (added 2026-10-05), `start_print` for an approved job after a person
confirms the plate is empty, and `stop_print`. On 2026-10-20 a human approved
the external spool (`vt_tray`) as a `start_print` mapping target for printers
without an AMS, checked like an AMS tray, and the one `M500` in Bambu's stock
start gcode (saving the bed mesh right after `G29`) as accepted by the gcode
check. On 2026-10-21 a view-only camera (`GET /printers/{id}/camera.jpg`, no
claim, one shared read per printer every 2 s, off on the page until a person
turns it on, never plate-check evidence) was approved, and with it the manual
snapshot step was dropped: a camera start records its own frame under the
claim when the person presses start. Homing was considered and
deliberately not added: it moves the bed into whatever is on the plate. On
2026-11-02 Yang approved (relayed in PR #8) **agent mode for the Bambu A1
only**: an admin-controlled, audited switch, off by default, that lets an agent
submit, approve and start jobs on `bambu_a1_01`, deciding itself from a claimed
camera snapshot that the plate is empty (`agent_camera`). Every other printer,
and the A1 with the switch off, keeps the human-in-the-loop rules unchanged; see
`AGENT_RULES.md` rule 4. The same day, robot calibration was ruled out of scope:
the gateway reports object positions in bed millimetres and the arm agent owns
the transform. On 2026-11-05 Yang approved (in a Claude Code session, recorded
in this change) a fifth verb, **`position_bed`, for the Bambu A1 only**: a
Y-only move of the bed to one of two configured positions -- `inspect`, where
the camera sees the whole plate, and `pickup`, where the arm takes parts off --
on an idle printer, under the claim, with a verified caller (an agent also
needs agent mode on) who states `bed_path_clear: true`. No X, no Z, no homing,
no caller-supplied coordinate; never retried. See `AGENT_RULES.md` rule 1. All printer commands go through `bambu_server.dispatch` and
the backend's control methods; nothing else may call `bambulabs_api` command
methods. Adding a verb, relaxing a gate, or letting an agent start a print
without a person anywhere beyond that A1 approval needs a new explicitly
approved design. Routing control through `lab-skills` is still owed.

## Working conventions

- Use `uv`: `uv sync --extra dev`, then `uv run pytest -q` and `uv run ruff check .`.
- Keep HTTP status handlers side-effect-free. Printer I/O belongs in the
  background monitor; routes only read its cache.
- Keep credentials, printer addresses, serial numbers, and site-specific
  configuration in `.env` and `printers.local.yaml`, both gitignored.
- Never expose access codes, serial numbers, printer addresses, or raw MQTT
  payloads through status responses or logs.
- Treat a printer that cannot be reached as `unknown`, not `error`. Reserve
  `error` for a reachable printer reporting an operational failure.
- Tests must not touch physical printers. Use fake backends.
