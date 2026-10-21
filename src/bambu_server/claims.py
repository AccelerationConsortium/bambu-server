"""STATUS_SPEC §5 cooperative claims, one registry per printer.

A claim is a cheap optimistic lock: a session asks for it, keeps it alive with
heartbeats, and releases it when done. The gateway enforces it hard on every
``/control/*`` route (``X-Claim-Token`` or 423), because the one action those
routes exist for -- starting a print -- is exactly the kind of thing two callers
must not race on.

Claims are cooperative, not authenticated (spec §5): the token proves that the
caller is the session that claimed, not who that session is. Identity comes
from the trusted edge (:mod:`bambu_server.identity`) and is recorded separately.

Expiry is evaluated lazily on every access rather than by a background reaper:
there is nothing to do when a claim lapses except stop honouring it, and
checking the clock at the moment of use cannot drift from a timer.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sdl_lab_contract.claims import ClaimedBy, ClaimRequest, ClaimResponse

#: Bounds the device applies to a requested TTL (spec: "device may clamp").
MIN_TTL_S = 5.0
MAX_TTL_S = 300.0


class ClaimConflict(RuntimeError):
    """Another session holds the claim."""

    def __init__(self, holder: ClaimedBy) -> None:
        super().__init__(f"claimed by {holder.owner} until {holder.expires_at.isoformat()}")
        self.holder = holder


class ClaimLost(RuntimeError):
    """The presented token matches no live claim."""


@dataclass
class _Claim:
    token: str
    owner: str
    session_id: str
    ttl_s: float
    expires_at: datetime

    def holder(self) -> ClaimedBy:
        return ClaimedBy(session_id=self.session_id, owner=self.owner, expires_at=self.expires_at)


class ClaimRegistry:
    """The claim state of one printer."""

    def __init__(self) -> None:
        self._claim: _Claim | None = None

    # -- reads -------------------------------------------------------------

    def holder(self, now: datetime | None = None) -> ClaimedBy | None:
        """Who holds the claim right now, or ``None`` (including after expiry)."""

        claim = self._live(now or datetime.now(UTC))
        return claim.holder() if claim else None

    def verify(self, token: str | None, now: datetime | None = None) -> bool:
        """True when ``token`` is the live claim's token."""

        claim = self._live(now or datetime.now(UTC))
        return bool(claim and token and secrets.compare_digest(claim.token, token))

    # -- writes ------------------------------------------------------------

    def claim(self, request: ClaimRequest, now: datetime | None = None) -> ClaimResponse:
        """Acquire the claim, or raise :class:`ClaimConflict`.

        A repeat request from the session that already holds it is idempotent:
        it extends the claim and returns a fresh token (the spec allows either).
        """

        now = now or datetime.now(UTC)
        current = self._live(now)
        if current is not None and current.session_id != request.session_id:
            raise ClaimConflict(current.holder())
        ttl = min(max(float(request.ttl_s), MIN_TTL_S), MAX_TTL_S)
        claim = _Claim(
            token=secrets.token_urlsafe(32),
            owner=request.owner,
            session_id=request.session_id,
            ttl_s=ttl,
            expires_at=now + timedelta(seconds=ttl),
        )
        self._claim = claim
        return ClaimResponse(
            claim_token=claim.token,
            # Half the TTL leaves a missed beat before the claim lapses.
            heartbeat_interval_s=ttl / 2,
            expires_at=claim.expires_at,
        )

    def heartbeat(self, token: str | None, now: datetime | None = None) -> datetime:
        """Extend the claim and return the new expiry, or raise :class:`ClaimLost`."""

        now = now or datetime.now(UTC)
        if not self.verify(token, now):
            raise ClaimLost("no live claim matches the presented token")
        assert self._claim is not None
        self._claim.expires_at = now + timedelta(seconds=self._claim.ttl_s)
        return self._claim.expires_at

    def release(self, token: str | None, now: datetime | None = None) -> None:
        """Release the claim. Idempotent: an unknown token is not an error."""

        if self.verify(token, now):
            self._claim = None

    # -- internals ---------------------------------------------------------

    def _live(self, now: datetime) -> _Claim | None:
        claim = self._claim
        if claim is None:
            return None
        if claim.expires_at <= now:
            self._claim = None
            return None
        return claim
