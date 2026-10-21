"""STATUS_SPEC §5 claim registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sdl_lab_contract.claims import ClaimRequest

from bambu_server.claims import MAX_TTL_S, MIN_TTL_S, ClaimConflict, ClaimLost, ClaimRegistry

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _req(session: str = "a", ttl: float = 30.0) -> ClaimRequest:
    return ClaimRequest(owner=f"owner-{session}", session_id=session, ttl_s=ttl)


def test_claim_expires_without_a_heartbeat() -> None:
    claims = ClaimRegistry()
    response = claims.claim(_req(ttl=30), now=T0)
    assert claims.verify(response.claim_token, now=T0 + timedelta(seconds=29))
    assert not claims.verify(response.claim_token, now=T0 + timedelta(seconds=31))
    assert claims.holder(now=T0 + timedelta(seconds=31)) is None


def test_heartbeat_extends_and_a_stale_token_is_lost() -> None:
    claims = ClaimRegistry()
    token = claims.claim(_req(ttl=30), now=T0).claim_token
    new_expiry = claims.heartbeat(token, now=T0 + timedelta(seconds=20))
    assert new_expiry == T0 + timedelta(seconds=50)
    with pytest.raises(ClaimLost):
        claims.heartbeat("someone-elses", now=T0 + timedelta(seconds=21))


def test_another_session_conflicts_until_expiry() -> None:
    claims = ClaimRegistry()
    claims.claim(_req("a"), now=T0)
    with pytest.raises(ClaimConflict) as raised:
        claims.claim(_req("b"), now=T0 + timedelta(seconds=1))
    assert raised.value.holder.session_id == "a"
    claims.claim(_req("b"), now=T0 + timedelta(seconds=31))


def test_same_session_reclaim_is_idempotent_and_rotates_the_token() -> None:
    claims = ClaimRegistry()
    first = claims.claim(_req("a"), now=T0).claim_token
    second = claims.claim(_req("a"), now=T0 + timedelta(seconds=1)).claim_token
    assert first != second
    assert claims.verify(second, now=T0 + timedelta(seconds=2))
    assert not claims.verify(first, now=T0 + timedelta(seconds=2))


def test_ttl_is_clamped_and_release_is_idempotent() -> None:
    claims = ClaimRegistry()
    short = claims.claim(_req(ttl=0.1), now=T0)
    assert short.expires_at == T0 + timedelta(seconds=MIN_TTL_S)
    claims.release(short.claim_token)
    claims.release(short.claim_token)
    long = claims.claim(_req(ttl=10_000), now=T0)
    assert long.expires_at == T0 + timedelta(seconds=MAX_TTL_S)
