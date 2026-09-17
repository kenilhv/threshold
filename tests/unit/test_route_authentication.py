"""Every route knows who is asking, or says out loud why it does not need to.

`GET /v1/documents/{id}` and `GET /v1/documents/{id}/match` shipped with no
authentication at all -- not weak authorisation, none -- from M1 until M9. An
anonymous caller got the vendor's name, the invoice amount, the free text of
every exception, and from M8 the payment's state and its rail reference. The
queue beside them returned 401 the whole time, which is how it went unnoticed:
the routes that *looked* sensitive were guarded, and the ones that returned the
same data one document at a time were not.

Nobody decided that. `require_principal` is a dependency each route opts into,
and a route that forgets is indistinguishable from one that chose not to. So the
choice is made explicit here: a route either depends on `require_principal`, or
it appears in `PUBLIC` with the reason written next to it. A new route that does
neither fails this test, which is the only point at which anybody is certain to
be looking.

The walk is recursive because a principal can arrive through a sub-dependency,
and it reuses `test_session_scope`'s route finder because that one already
learned, the hard way, that FastAPI 0.141 nests included routers where a flat
scan finds nothing.
"""

from __future__ import annotations

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from tests.unit.test_session_scope import _routes

from threshold.api.auth import require_principal

#: Routes that answer without knowing who is asking, and why that is right.
#:
#: Keyed on (method, path). An entry here is a security decision and should read
#: like one.
PUBLIC: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "liveness; an orchestrator has no token and must not need one",
    ("GET", "/readyz"): "readiness; same caller, same reason",
    ("POST", "/v1/documents"): (
        "invoices arrive from suppliers, who are not users of this system. The door "
        "is open on purpose and everything behind it assumes a hostile document: the "
        "match, the bank-detail comparison and the duplicate check are the controls, "
        "not the upload. Reading back what was submitted is a different matter, and "
        "is not public."
    ),
}


def _is_authenticated(dependant: Dependant) -> bool:
    """Whether `require_principal` appears anywhere beneath this dependant."""
    for dependency in dependant.dependencies:
        if dependency.call is require_principal or _is_authenticated(dependency):
            return True
    return False


def _surface() -> dict[tuple[str, str], APIRoute]:
    """Every (method, path) the application serves, excluding the framework's own."""
    found: dict[tuple[str, str], APIRoute] = {}
    for route in _routes():
        if not route.include_in_schema:
            continue  # /docs, /openapi.json and friends
        for method in route.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            found[(method, route.path)] = route
    return found


def test_every_route_is_authenticated_or_deliberately_public() -> None:
    unexplained = sorted(
        f"{method} {path}"
        for (method, path), route in _surface().items()
        if not _is_authenticated(route.dependant) and (method, path) not in PUBLIC
    )
    assert not unexplained, (
        f"these routes answer anybody and nobody has said why: {unexplained}. Add "
        "`require_principal`, or add the route to PUBLIC with the reason it is safe."
    )


def test_nothing_is_both_public_and_authenticated() -> None:
    """An allowlist entry for a route that is in fact guarded is a stale claim,
    and the next reader will believe it."""
    contradictory = sorted(
        f"{method} {path}"
        for (method, path), route in _surface().items()
        if (method, path) in PUBLIC and _is_authenticated(route.dependant)
    )
    assert not contradictory, f"listed as public but require a principal: {contradictory}"


def test_the_public_list_names_routes_that_exist() -> None:
    """The other way an allowlist rots: the route is renamed and the exemption
    stays, waiting to apply to whatever is given that path next."""
    missing = sorted(f"{m} {p}" for (m, p) in PUBLIC if (m, p) not in _surface())
    assert not missing, f"PUBLIC exempts routes that are not served: {missing}"


def test_every_public_route_says_why() -> None:
    for (method, path), reason in PUBLIC.items():
        assert len(reason.split()) >= 5, f"{method} {path} is public without an argument"


def test_the_walk_found_the_application() -> None:
    """The vacuity guard. Zero routes satisfy every assertion above, and a
    surface where nothing is authenticated means the detector is broken rather
    than the application being open."""
    surface = _surface()
    assert len(surface) >= 8, f"only found {sorted(surface)}"
    guarded = [key for key, route in surface.items() if _is_authenticated(route.dependant)]
    assert len(guarded) >= 3, "the walk cannot see `require_principal` at all"
    assert ("GET", "/v1/queue") in guarded, "the queue is the known-guarded route"
