# Agent rules — bambu-server

The canonical lab-wide rules in `../ac-organic-lab/docs/AGENT_RULES.md` apply
first and in full. The authoritative device contract is
`../ac-organic-lab/docs/STATUS_SPEC.md`. This file only adds rules specific to
the Bambu printer gateway.

1. Control is limited to what a human approved on 2026-10-04: cooperative
   claims, a camera snapshot, the chamber light (approved 2026-10-05),
   `start_print` of an approved job (from an AMS tray or, approved 2026-10-20,
   the external spool, checked the same way; and, approved the same day, with
   the stock start gcode's bed-mesh `M500` right after `G29` accepted -- every
   other refused command still refused), and
   `stop_print`, all behind `dispatch.enabled` and the gates in
   `bambu_server.dispatch`. **Added 2026-11-05, approved by Yang, Bambu A1
   only:** `position_bed`, which moves the bed (Y axis only, absolute `G90` /
   `G1 Y.. F..`, nothing else) to a configured `inspect` or `pickup` position
   (`bed_positions` on the printer, accepted by config on an A1 only). It needs
   the claim, an idle printer in no error state with no job in flight, an
   edge-verified caller -- an agent also needs the A1's agent mode on -- and the
   caller's statement `bed_path_clear: true` that no robot arm, hand or object
   is in the bed's travel or above the plate. The caller names a position,
   never a coordinate; a refused or undelivered move is never retried and is
   never "fixed" by homing. Any other verb, a relaxed gate, or integration with
   `lab-skills` plan execution needs a new, explicitly approved design.
2. Monitoring must never start, pause, resume, stop, heat, home, move, calibrate,
   upload to, or otherwise control a printer.
3. The service may request MQTT state refreshes in its background monitoring
   loop. A status, profile, or queue request must never cause printer I/O; only
   a claimed `/control/*` request may, with one exception approved 2026-10-21:
   the view-only `GET /printers/{id}/camera.jpg`, present only with
   `dispatch.enabled`, which reads the camera port (never publishes a command),
   at most once per printer every 2 s shared by all viewers, and whose frames
   are never plate-check evidence. The page loads with it off; a person turns
   it on.
4. A print is started only by a person who has confirmed the build plate is
   empty. (Since 2026-10-21, approved, a camera confirmation needs no separate
   snapshot step: the person checks the live view and the gateway records a
   frame under the claim when they press start, refusing if it cannot.) An agent must never set `plate_confirmed_empty`, start a print, or
   approve a job on its own initiative, and must never retry a failed or
   uncertain dispatch -- a person checks the printer first.
   **One exception, approved by Yang on 2026-11-02 (relayed in PR #8):** on the
   Bambu A1 (`bambu_a1_01`, the only model config accepts
   `agent_mode_eligible` on), while an edge-verified admin has switched its
   agent mode on, an agent may submit, approve and start jobs and may itself
   decide the plate is empty from a camera snapshot it took under its claim
   (`plate_check_method: agent_camera`, frame at most
   `dispatch.agent_snapshot_max_age_s` old, deciding agent and rationale
   recorded, frame archived with its SHA-256). The switch is off by default,
   only an admin can flip it, and every flip is audited. Nothing else changes:
   on every other printer, and on the A1 with the switch off, the rule above
   holds in full, and no agent ever retries a failed or uncertain dispatch.
   Widening this to another printer needs a new approval.
5. Printer access codes, serial numbers, addresses, and raw MQTT payloads are
   local secrets and must not be committed, returned by HTTP, or logged.
6. Tests and development defaults must not contact lab hardware.
