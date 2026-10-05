# Agent rules — bambu-server

The canonical lab-wide rules in `../ac-organic-lab/docs/AGENT_RULES.md` apply
first and in full. The authoritative device contract is
`../ac-organic-lab/docs/STATUS_SPEC.md`. This file only adds rules specific to
the Bambu printer gateway.

1. Control is limited to what a human approved on 2026-10-04: cooperative
   claims, a camera snapshot, `start_print` of an approved job, and
   `stop_print`, all behind `dispatch.enabled` and the gates in
   `bambu_server.dispatch`. Any other verb, a relaxed gate, or integration with
   `lab-skills` plan execution needs a new, explicitly approved design.
2. Monitoring must never start, pause, resume, stop, heat, home, move, calibrate,
   upload to, or otherwise control a printer.
3. The service may request MQTT state refreshes in its background monitoring
   loop. A status, profile, or queue request must never cause printer I/O; only
   a claimed `/control/*` request may.
4. A print is started only by a person who has confirmed the build plate is
   empty. An agent must never set `plate_confirmed_empty`, start a print, or
   approve a job on its own initiative, and must never retry a failed or
   uncertain dispatch -- a person checks the printer first.
5. Printer access codes, serial numbers, addresses, and raw MQTT payloads are
   local secrets and must not be committed, returned by HTTP, or logged.
6. Tests and development defaults must not contact lab hardware.
