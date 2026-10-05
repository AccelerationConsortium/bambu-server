# Moving the P1S to LAN Only + Developer Mode — plan

**Status:** PLANNED, not started (decided 2026-10-05). Owner: Yang Cao.
**Scope:** `bambu_p1s_01` only. The H2D stays as it is until the P1S has run
cleanly in this mode.

## Why

The control plane (`docs/CONTROL_PLANE_DESIGN.md`) is deployed and its first
live start (submission `b3cdb589`, 2026-10-05 02:19 UTC) did not print:

- the `.3mf` upload over FTPS succeeded and was size-verified (47,584 bytes);
- the `project_file` start command was published and acknowledged by the
  printer's MQTT broker;
- the printer stayed idle (`gcode_state` FINISH, bed and nozzle cold), and the
  job failed after 180 s with `dispatch.uncertainty` recorded.

The P1S runs firmware **01.10.00.00**. Since Bambu's 2025 authorization
change, a printer that is not in LAN Only Mode keeps publishing telemetry to
local MQTT clients but ignores their control commands; local third-party
control requires **LAN Only Mode + Developer Mode**. This is the most likely
cause, but it is **not yet confirmed**: the gateway does not record the
printer's reply to a command, so the refusal itself was never observed.

## What changes for people (decide and announce first)

- **Lost:** Bambu Handy remote monitoring and printing, Bambu cloud printing
  from Bambu Studio / MakerWorld, cloud print history, and over-the-air
  firmware notifications for this printer.
- **Kept:** printing from Bambu Studio on the lab network in LAN mode (needs
  the printer's IP and access code), the printer's touchscreen, SD-card
  printing, the gateway's monitoring, camera snapshot and control.
- **Firmware updates** become manual (Bambu Studio in LAN mode, or SD card).
  Do not update firmware without re-running the verification below: a firmware
  release can change the local command surface again.
- The decision recorded in `docs/TODO.md` ("LAN Only Mode remains off,
  preserving cloud control") is reversed for the P1S; update that line when
  the move is done.

## Plan

### 0. Before touching the printer (software, no printer I/O)

1. **Record command replies.** Make the backend capture the printer's response
   to each command (the MQTT report carries `command`, `result` and `reason`
   for `project_file`, `ledctrl`, `stop`), store it on the dispatch record, and
   surface it in the 502/failed note. Without this, a refusal after the move
   would look exactly like today's silent failure.
2. **Confirm the diagnosis cheaply, while still in cloud mode.** With reply
   capture deployed, toggle the chamber light from `/bambu/ui/` (a command with
   no motion and no heat). A refusal reason naming authorization confirms the
   cause before anyone gives up cloud access. If the light command is
   *accepted*, stop: the problem is elsewhere (start payload, firmware field
   requirements) and this migration may not be needed.
3. **Pin the address.** The gateway, the API unit's egress allowlist and Bambu
   Studio all reach the printer by IP. Get a DHCP reservation for the P1S (or
   confirm its address is stable) before LAN-only removes the cloud fallback.

### 1. Switch modes (at the printer, with a person present)

4. Announce a short window to everyone who uses the P1S.
5. On the printer touchscreen, in its network settings: turn **LAN Only Mode**
   on, then turn **Developer Mode** on. (Verify the exact menu path against
   Bambu's wiki for firmware 01.10 before the window; it has moved between
   releases.)
6. Read the **access code** shown on the screen. If it changed, update
   `BAMBU_P1S_01_ACCESS_CODE` in `/etc/device-gateway-staging/bambu.env`
   (root-owned; keep a backup), then `sudo systemctl restart bambu-server`.

### 2. Verify, lowest risk first

7. `GET /bambu/printers/bambu_p1s_01/status`: telemetry fresh, `activity`
   idle, `allowed_actions` includes `light`, `snapshot`, `start_print`.
8. Toggle the chamber light from `/bambu/ui/`; confirm the reply is accepted
   and `details.light_state` follows.
9. Take a camera snapshot.
10. First print: a 20 mm PLA cube STL, sliced by the gateway, approved and
    started by a signed-in person **standing at the printer**, plate checked in
    person, standard AMS tray. Watch it reach `running`, then `finished`.
11. Check that the printer's reported job name matches the uploaded
    `gw_<id>_*.3mf` (the gateway accepts a state change if it does not, and
    notes that in the job history).

### 3. Afterwards

12. Update `docs/TODO.md` (live printer configuration) and the README's
    control-plane section with the mode the printer now runs in.
13. Tell Bambu Studio users how to add the P1S in LAN mode (IP + access code,
    shared through the lab's usual private channel, never in a repo).
14. Delete the stale `gw_*.3mf` files left on the printer's storage by the
    failed attempts, from the printer's file manager.

## Rollback

Turn Developer Mode and LAN Only Mode off on the printer (re-binding to the
Bambu account if it asks), restore the access code in `bambu.env` if it
changed, and restart the gateway. Monitoring keeps working in cloud mode;
starts from the gateway will be refused again, and every such attempt fails
safely with its uncertainty recorded.

## Out of scope

- The H2D (different camera protocol, no declared plate yet).
- AMS HT tray mapping (refused by the gate until its tray index is verified).
- Homing / motion verbs (declined; see `docs/CONTROL_PLANE_DESIGN.md`).
