"""Operator identity injected by a trusted edge.

This service has no login of its own. When it is fronted by the lab's single
Caddy edge, that edge authenticates the person (``forward_auth`` against
``ac_auth``) and injects who they are as request headers. This module decides
whether to believe those headers.

The rule, copied from the xArm's arrangement because the reasoning is the same:
the injected identity is trusted **only** when the request also carries a shared
secret that the edge holds and a direct caller cannot produce. The gateway's
port stays reachable on the tailnet, so without that check anyone could claim to
be anyone by setting a header.

Two properties matter more than convenience here:

* **Fail closed.** No configured secret means no trusted identity, ever --
  never "trust the header because we have nothing to check it against". A
  deployment that has not been given a secret behaves exactly as it did before
  this module existed.
* **Constant-time comparison.** Comparing secrets with ``==`` leaks their
  contents through timing, one byte at a time.
"""

from __future__ import annotations

import hmac
import re

from pydantic import BaseModel

#: Injected by the edge after it has authenticated the person.
USER_HEADER = "X-Auth-User"
ROLE_HEADER = "X-Auth-Role"
#: Proof that the request came through the edge and not straight off the tailnet.
EDGE_AUTH_HEADER = "X-Edge-Auth"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_LEN = 120


class Actor(BaseModel):
    """Who the service believes is making a request.

    ``verified`` is the whole point: an unverified actor is a self-declared
    label, and the difference is recorded on every job rather than blurred.
    """

    user: str | None = None
    role: str | None = None
    verified: bool = False


ANONYMOUS = Actor()


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _CONTROL_CHARS.sub("", value).strip()[:_MAX_LEN]
    return cleaned or None


def resolve_actor(headers: object, *, edge_secret: str | None) -> Actor:
    """Resolve the caller from request headers.

    ``headers`` is anything with a case-insensitive ``get`` (Starlette's
    ``Headers``). Returns :data:`ANONYMOUS` unless an edge-verified identity is
    present, so a caller can only ever *gain* attribution by coming through the
    edge -- never lose a check by omitting a header.
    """

    if not edge_secret:
        # Nothing to verify against. Deliberately not "trust the header".
        return ANONYMOUS

    getter = getattr(headers, "get", None)
    if getter is None:  # pragma: no cover - defensive
        return ANONYMOUS

    presented = getter(EDGE_AUTH_HEADER)
    if not presented or not hmac.compare_digest(str(presented), edge_secret):
        return ANONYMOUS

    user = _clean(getter(USER_HEADER))
    if user is None:
        # The edge proved itself but named nobody: an authenticated request with
        # no subject is not an identity.
        return ANONYMOUS
    return Actor(user=user, role=_clean(getter(ROLE_HEADER)), verified=True)


def actor_for(actor: Actor, supplied: str | None) -> tuple[str, bool]:
    """Choose the name to record, and say whether it was verified.

    A verified identity always wins over whatever the client typed -- otherwise
    a signed-in person could file work under someone else's name. Without one,
    the supplied label is used and marked unverified.
    """

    if actor.verified and actor.user:
        return actor.user, True
    cleaned = _clean(supplied)
    if cleaned is None:
        raise ValueError("no actor supplied and no verified identity available")
    return cleaned, False
