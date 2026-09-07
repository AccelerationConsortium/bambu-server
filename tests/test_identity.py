"""Edge-injected identity (bambu_server.identity)."""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from bambu_server.identity import (
    ANONYMOUS,
    EDGE_AUTH_HEADER,
    ROLE_HEADER,
    USER_HEADER,
    Actor,
    actor_for,
    resolve_actor,
)

SECRET = "edge-secret-value"


def _headers(**values: str) -> Headers:
    return Headers({k.replace("_", "-"): v for k, v in values.items()})


def _edge(user: str = "alice", role: str | None = "operator", secret: str = SECRET) -> Headers:
    raw = {EDGE_AUTH_HEADER: secret, USER_HEADER: user}
    if role:
        raw[ROLE_HEADER] = role
    return Headers(raw)


def test_a_verified_edge_request_yields_an_identity() -> None:
    actor = resolve_actor(_edge(), edge_secret=SECRET)

    assert actor == Actor(user="alice", role="operator", verified=True)


def test_no_configured_secret_trusts_nothing() -> None:
    """Fail closed: nothing to verify against must never mean "believe it"."""
    for secret in (None, "", "   "):
        assert resolve_actor(_edge(), edge_secret=secret) == ANONYMOUS


def test_a_wrong_or_missing_edge_secret_is_anonymous() -> None:
    assert resolve_actor(_edge(secret="wrong"), edge_secret=SECRET) == ANONYMOUS
    assert resolve_actor(
        _headers(**{USER_HEADER: "alice"}), edge_secret=SECRET
    ) == ANONYMOUS


def test_a_header_claim_without_the_edge_secret_is_ignored() -> None:
    """The port is reachable on the tailnet, so a bare header proves nothing."""
    forged = Headers({USER_HEADER: "admin", ROLE_HEADER: "admin"})

    assert resolve_actor(forged, edge_secret=SECRET) == ANONYMOUS


def test_a_verified_edge_naming_nobody_is_anonymous() -> None:
    """An authenticated request with no subject is not an identity."""
    for user in ("", "   ", "\x00\x01"):
        headers = Headers({EDGE_AUTH_HEADER: SECRET, USER_HEADER: user})
        assert resolve_actor(headers, edge_secret=SECRET) == ANONYMOUS


def test_the_role_is_optional() -> None:
    actor = resolve_actor(_edge(role=None), edge_secret=SECRET)

    assert actor.verified is True
    assert actor.user == "alice"
    assert actor.role is None


def test_control_characters_are_stripped_and_length_capped() -> None:
    headers = Headers({EDGE_AUTH_HEADER: SECRET, USER_HEADER: "a\x00lice" + "x" * 400})
    actor = resolve_actor(headers, edge_secret=SECRET)

    assert actor.user is not None
    assert "\x00" not in actor.user
    assert actor.user.startswith("alice")
    assert len(actor.user) <= 120


def test_a_verified_identity_overrides_a_supplied_name() -> None:
    """A signed-in person must not be able to act under someone else's name."""
    verified = Actor(user="alice", verified=True)

    assert actor_for(verified, "bob") == ("alice", True)
    assert actor_for(verified, None) == ("alice", True)


def test_without_a_verified_identity_the_supplied_name_is_used_unverified() -> None:
    assert actor_for(ANONYMOUS, "bob") == ("bob", False)
    assert actor_for(ANONYMOUS, "  bob  ") == ("bob", False)


def test_no_identity_and_no_name_is_an_error() -> None:
    for supplied in (None, "", "   "):
        with pytest.raises(ValueError):
            actor_for(ANONYMOUS, supplied)


def test_headers_without_a_getter_are_anonymous() -> None:
    assert resolve_actor(object(), edge_secret=SECRET) == ANONYMOUS
