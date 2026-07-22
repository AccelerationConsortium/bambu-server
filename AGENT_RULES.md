# Agent rules — bambu-server

The canonical lab-wide rules in `../ac-organic-lab/docs/AGENT_RULES.md` apply
first and in full. The authoritative device contract is
`../ac-organic-lab/docs/STATUS_SPEC.md`. This file only adds rules specific to
the Bambu printer gateway.

1. The service is read-only until a human explicitly approves a control-plane
   design integrated with `lab-skills`, cooperative claims, preconditions, and
   audited plan execution.
2. Monitoring must never start, pause, resume, stop, heat, home, move, calibrate,
   upload to, or otherwise control a printer.
3. The service may request MQTT state refreshes in its background monitoring
   loop. A dashboard request must never cause printer I/O.
4. Printer access codes, serial numbers, addresses, and raw MQTT payloads are
   local secrets and must not be committed, returned by HTTP, or logged.
5. Tests and development defaults must not contact lab hardware.
