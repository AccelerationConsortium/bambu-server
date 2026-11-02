"""Agent mode: an admin-controlled switch letting agents run a printer end to end.

Approved by Yang on 2026-11-02 for the Bambu A1 (``bambu_a1_01``) only. With the
switch on, an agent may submit, approve and start jobs on that printer and
decide for itself, from a claimed camera snapshot, that the plate is empty
(``plate_check_method: agent_camera``). With it off -- the default -- or on any
other printer, agent submissions, approvals and starts are refused, and the
human-in-the-loop path is unchanged.

Three facts decide it:

* **eligibility** is static configuration (``agent_mode_eligible`` on the
  printer), and the config validator only accepts it on a Bambu A1;
* the **switch** is runtime state persisted under the submissions directory,
  flipped only by an edge-verified identity holding an admin role, with every
  flip appended to an audit log (who, role, when, reason, previous value);
* an **agent caller** is a request whose edge-verified role is an agent role, or
  that declares itself with ``X-Agent-Id``. A verified identity is always
  recorded alongside the declared agent id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .identity import Actor

logger = logging.getLogger(__name__)

#: Header an agent sends to identify itself (an opaque agent id).
AGENT_HEADER = "X-Agent-Id"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_A1_MODEL_RE = re.compile(r"^(?:bambu(?:\s+lab)?\s+)?a1$", re.IGNORECASE)


def model_is_agent_mode_capable(model: str | None) -> bool:
    """Only the Bambu A1 is approved for agent mode (not the A1 mini, not others)."""

    return bool(model and _A1_MODEL_RE.match(model.strip()))


class AgentCaller(BaseModel):
    """Whether a request comes from an agent, and which one."""

    is_agent: bool = False
    agent_id: str | None = None


def agent_caller(actor: Actor, headers: object, *, agent_roles: list[str]) -> AgentCaller:
    getter = getattr(headers, "get", None)
    raw = getter(AGENT_HEADER) if getter else None
    declared = _CONTROL_CHARS.sub("", str(raw)).strip()[:120] if raw else ""
    by_role = bool(actor.verified and actor.role and actor.role in agent_roles)
    if not declared and not by_role:
        return AgentCaller()
    return AgentCaller(is_agent=True, agent_id=declared or actor.user)


class AgentModeState(BaseModel):
    printer_id: str
    enabled: bool = False
    changed_by: str | None = None
    changed_by_role: str | None = None
    changed_at: datetime | None = None
    reason: str | None = None


class AgentModeAudit(BaseModel):
    printer_id: str
    enabled: bool
    previous: bool
    changed_by: str
    changed_by_role: str | None = None
    changed_at: datetime
    reason: str


class AgentModeView(AgentModeState):
    eligible: bool
    audit: list[AgentModeAudit] = Field(default_factory=list)


class AgentModeStore:
    """Persisted switch state plus an append-only audit log."""

    def __init__(self, root: Path) -> None:
        self._state_path = root / "agent_mode.json"
        self._audit_path = root / "agent_mode_audit.jsonl"
        self._states: dict[str, AgentModeState] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            for value in payload if isinstance(payload, list) else []:
                state = AgentModeState.model_validate(value)
                self._states[state.printer_id] = state
        except (OSError, ValueError):
            # Fail closed: an unreadable switch file means every switch is off.
            logger.warning("Unreadable agent mode state; agent mode is off everywhere")
            self._states = {}

    def get(self, printer_id: str) -> AgentModeState:
        return self._states.get(printer_id) or AgentModeState(printer_id=printer_id)

    def audit(self, printer_id: str, limit: int = 20) -> list[AgentModeAudit]:
        if not self._audit_path.is_file():
            return []
        entries: list[AgentModeAudit] = []
        with self._audit_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = AgentModeAudit.model_validate_json(line)
                except ValueError:
                    continue
                if entry.printer_id == printer_id:
                    entries.append(entry)
        return entries[-limit:][::-1]

    async def set(
        self, printer_id: str, *, enabled: bool, changed_by: str, role: str | None, reason: str
    ) -> AgentModeState:
        async with self._lock:
            previous = self.get(printer_id)
            now = datetime.now(UTC)
            entry = AgentModeAudit(
                printer_id=printer_id, enabled=enabled, previous=previous.enabled,
                changed_by=changed_by, changed_by_role=role, changed_at=now, reason=reason,
            )
            state = AgentModeState(
                printer_id=printer_id, enabled=enabled, changed_by=changed_by,
                changed_by_role=role, changed_at=now, reason=reason,
            )
            states = {**self._states, printer_id: state}
            # The audit line is written first: a flip that is not audited must
            # not take effect.
            await asyncio.to_thread(self._write, entry, list(states.values()))
            self._states = states
            logger.info(
                "Agent mode on %s set to %s by %s (%s)",
                printer_id, "on" if enabled else "off", changed_by, role or "no role",
            )
            return state

    def _write(self, entry: AgentModeAudit, states: list[AgentModeState]) -> None:
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary = self._state_path.with_name(f"{self._state_path.name}.tmp")
        temporary.write_text(
            json.dumps([s.model_dump(mode="json") for s in states], indent=2), encoding="utf-8"
        )
        os.replace(temporary, self._state_path)


class AgentModeRefusal(Exception):
    def __init__(self, status: int, body: dict[str, object]) -> None:
        super().__init__(str(body.get("detail")))
        self.status = status
        self.body = body


class AgentModeGate:
    """The one predicate every agent submission, approval and start goes through."""

    def __init__(self, store: AgentModeStore, eligible: set[str]) -> None:
        self.store = store
        self.eligible = eligible

    def view(self, printer_id: str) -> AgentModeView:
        state = self.store.get(printer_id)
        return AgentModeView(
            **state.model_dump(),
            eligible=printer_id in self.eligible,
            audit=self.store.audit(printer_id),
        )

    def summary(self, printer_id: str) -> dict[str, object]:
        state = self.store.get(printer_id)
        eligible = printer_id in self.eligible
        return {
            "eligible": eligible,
            "enabled": eligible and state.enabled,
            "changed_by": state.changed_by,
            "changed_at": state.changed_at.isoformat() if state.changed_at else None,
            "reason": state.reason,
        }

    def require(self, printer_id: str, action: str) -> None:
        if printer_id not in self.eligible:
            raise AgentModeRefusal(403, {
                "detail": f"agent {action} is not available for {printer_id}: agent mode is "
                "approved for the Bambu A1 only; a person must do this here",
                "agent_mode": "not_eligible",
                "printer_id": printer_id,
            })
        if not self.store.get(printer_id).enabled:
            raise AgentModeRefusal(403, {
                "detail": f"agent {action} is refused: agent mode is off for {printer_id}; "
                "an admin must switch it on first",
                "agent_mode": "off",
                "printer_id": printer_id,
            })
