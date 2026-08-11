# AC Bambu Server

A monitoring-only FastAPI gateway for the Bambu Lab printers in the lab. It
uses [`bambulabs_api`](https://github.com/BambuTools/bambulabs_api) for local
MQTT telemetry and publishes one
[AC lab STATUS_SPEC v1.2](../ac-organic-lab/docs/STATUS_SPEC.md) surface per
printer. This repo conforms to lab status spec v1.2 on its per-printer
surfaces; the aggregate gateway envelope stays on v1.0 (it fronts printers and
has no primary operation of its own).

The service deliberately exposes **no control endpoints** in v0.1. The
third-party package supports commands, but those methods are isolated behind a
narrow monitoring adapter and are not reachable from HTTP. Future control work
must go through `lab-skills`, claims, preconditions, audited plans, and the
human-approval rules in the lab contract.

## Architecture

```text
Bambu printers -- local MQTT/TLS --> background monitors --> cached status
                                                              |
Lab dashboard ---------------- HTTP GET /printers/{id}/status -+
```

Dashboard requests only read the cache. They never connect to a printer or
request a telemetry refresh. The background monitor starts only the MQTT client;
camera and FTP clients are not started.

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

Per-printer STATUS_SPEC routes:

| Method | Path |
|---|---|
| GET | `/printers/{id}/` |
| GET | `/printers/{id}/health` |
| GET | `/printers/{id}/status` |
| GET | `/openapi.json` |

No `/control/*` routes exist.

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

## Dependency note

The initial integration targets `bambulabs_api` 2.6.x (`>=2.6.6,<3`). It uses
only `Printer.mqtt_start()`, `Printer.mqtt_stop()`, telemetry getters, and the
MQTT message callback. The version cap makes a future breaking major upgrade an
explicit review.
