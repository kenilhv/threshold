"""Bearer-token authentication against the OIDC issuer.

Before this, every endpoint in this service was open, including the ones this
milestone is about to add for authorising payment. The realm had been committed
since M0 and nothing consumed it.

What is verified, and why each one is not optional:

* **The signature, against a key fetched from the issuer.** Everything else
  rests on this.
* **The algorithm, chosen by us.** ``algorithms=["RS256"]`` is passed
  explicitly, so the token's own ``alg`` header cannot select the verification
  method. The attack it prevents is old and still works against anything that
  trusts the header: sign with ``HS256`` using the *public* key as the shared
  secret, and a verifier that reads ``alg`` from the token accepts it.
  ``alg: none`` is the same shape.
* **The issuer, exactly.** A token from another realm on the same Keycloak is a
  valid, correctly signed token — and it is not for us.
* **The audience contains this API.** Without it, any token any client in the
  realm holds is accepted here, including one minted for a different service
  that happens to trust a weaker login.
* **Expiry and not-before**, with a small leeway for clock skew between the
  issuer and this process.

Roles are read from ``realm_access.roles`` and anything unrecognised is dropped
rather than carried. A principal holding a role this system has no concept of is
a principal holding nothing, which is the safe direction.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final

import anyio
import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK, PyJWKSet

from threshold.config.settings import Settings, get_settings
from threshold.domain.roles import Role

log = structlog.get_logger(__name__)

#: Only RS256. Passed to the decoder explicitly, never read from the token.
ALLOWED_ALGORITHMS: Final = ["RS256"]

#: Clock skew tolerance between the issuer and this process.
LEEWAY_SECONDS: Final = 30

#: An unknown `kid` triggers at most one refetch this often, per process. Key
#: rotation is real and a signing key can be genuinely new; an unbounded refetch
#: on unknown kid is a denial-of-service handed to anyone who can send a token
#: with a made-up key id.
JWKS_MIN_REFETCH_SECONDS: Final = 60

_bearer = HTTPBearer(auto_error=False, description="An OIDC access token.")


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making this request, as far as the issuer is concerned."""

    subject: str
    username: str
    roles: frozenset[Role]

    def has(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))


class _JwksCache:
    """Signing keys, cached by key id.

    Fetched with urllib on a worker thread rather than with an async client:
    this runs at most once per key rotation, and adding an HTTP client to the
    dependency graph to save a thread hop on a cold path is not a trade worth
    making.
    """

    def __init__(self) -> None:
        self._keys: dict[str, PyJWK] = {}
        self._last_fetch: float = 0.0

    def clear(self) -> None:
        self._keys.clear()
        self._last_fetch = 0.0

    @property
    def key_count(self) -> int:
        """How many signing keys this instance can currently validate against.

        Read by `/readyz`, which reports it and never acts on it. Zero means no
        token has been validated yet -- the keys are fetched lazily on the first
        authenticated request -- not that the identity provider is down.
        """
        return len(self._keys)

    @property
    def seconds_since_refresh(self) -> float:
        """Age of the cached key set. `inf` when nothing has been fetched.

        Monotonic, like the refresh it measures, so a host clock correction
        cannot make the cache look newly fetched or impossibly old.
        """
        if self._last_fetch == 0.0:
            return float("inf")
        return time.monotonic() - self._last_fetch

    async def key_for(self, kid: str, url: str) -> PyJWK:
        cached = self._keys.get(kid)
        if cached is not None:
            return cached

        now = time.monotonic()
        if self._keys and now - self._last_fetch < JWKS_MIN_REFETCH_SECONDS:
            # Recently refreshed and still unknown. Refetching per request would
            # let any caller drive load at the issuer with invented key ids.
            raise _unauthorised("the token was signed with an unknown key")

        await self._refresh(url)
        refreshed = self._keys.get(kid)
        if refreshed is None:
            raise _unauthorised("the token was signed with an unknown key")
        return refreshed

    async def _refresh(self, url: str) -> None:
        try:
            raw = await anyio.to_thread.run_sync(_fetch, url)
        except Exception as exc:
            # Never fall through to accepting the token. If the keys cannot be
            # reached, the honest answer is that this service cannot currently
            # authenticate anyone -- which is a 503, not a 401 and certainly not
            # a pass.
            log.error("auth.jwks_unreachable", url=url, error=str(exc))
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "the identity provider's signing keys could not be reached",
            ) from exc

        self._keys = {key.key_id: key for key in PyJWKSet.from_dict(raw).keys if key.key_id}
        self._last_fetch = time.monotonic()
        log.info("auth.jwks_refreshed", keys=len(self._keys))


def _fetch(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        payload: dict[str, Any] = json.loads(response.read())
    return payload


_jwks = _JwksCache()


def jwks_cache() -> _JwksCache:
    """Exposed so tests can reset it between cases."""
    return _jwks


def _unauthorised(detail: str) -> HTTPException:
    # WWW-Authenticate is not decoration: it is what tells a client this was an
    # authentication failure it could retry with a token, rather than a refusal.
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _roles_from(claims: dict[str, Any]) -> frozenset[Role]:
    raw = claims.get("realm_access", {})
    names = raw.get("roles", []) if isinstance(raw, dict) else []
    known: set[Role] = set()
    for name in names:
        try:
            known.add(Role(name))
        except ValueError:
            # A realm role this system has no concept of. Dropped, not carried:
            # the alternative is an authorisation surface defined in Keycloak's
            # admin console rather than in this repository.
            continue
    return frozenset(known)


async def require_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Validate the bearer token and return who sent it."""
    if settings.oidc_issuer is None or settings.oidc_jwks_url is None:
        # Refuse rather than wave through. A deployment that forgot to configure
        # its issuer must not silently become an open API.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "this service is not configured to authenticate requests",
        )

    if credentials is None or not credentials.credentials:
        raise _unauthorised("an access token is required")

    token = credentials.credentials
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _unauthorised("the token is not a well-formed JWT") from exc

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise _unauthorised("the token carries no key id")

    key = await _jwks.key_for(kid, settings.oidc_jwks_url)

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key.key,
            algorithms=ALLOWED_ALGORITHMS,
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # The reason is logged, not returned. Telling a caller which check failed
        # is a slow oracle for shaping a token that passes more of them.
        log.info("auth.rejected", reason=type(exc).__name__, path=request.url.path)
        raise _unauthorised("the token was rejected") from exc

    subject = str(claims.get("sub", ""))
    if not subject:
        raise _unauthorised("the token carries no subject")

    return Principal(
        subject=subject,
        username=str(claims.get("preferred_username") or claims.get("email") or subject),
        roles=_roles_from(claims),
    )


def require_role(*allowed: Role) -> Any:
    """A dependency that additionally requires one of ``allowed``.

    403 rather than 401: the caller proved who they are and the answer is still
    no. Collapsing the two makes a permissions problem look like a login problem
    and sends the user to re-authenticate, which never helps.
    """

    async def _guard(principal: Principal = Depends(require_principal)) -> Principal:
        if not principal.has(*allowed):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "this action requires a role you do not hold",
            )
        return principal

    return _guard
