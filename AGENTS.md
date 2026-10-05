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
confirms the plate is empty, and `stop_print`. Homing was considered and
deliberately not added: it moves the bed into whatever is on the plate. All printer commands go through `bambu_server.dispatch` and
the backend's control methods; nothing else may call `bambulabs_api` command
methods. Adding a verb, relaxing a gate, or letting an agent start a print
without a person needs a new explicitly approved design. Routing control
through `lab-skills` is still owed.

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
