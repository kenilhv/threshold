"""Every route commits before it answers.

A `yield` dependency in FastAPI defaults to request scope, which runs its exit
code after the response has been sent. `get_session`'s exit code is the COMMIT,
so under the default an endpoint returns 201 and only then writes the row.

That was measured against this API, not assumed: approving an invoice and
reading the decisions back in the same breath came back empty in 6 of 8 trials,
and populated half a second later. The read-your-writes failure was the visible
half. The worse half was invisible — a commit that failed after the response had
gone would leave the caller holding a 201 for a decision that was never recorded.

`SessionDep` fixes it with `scope="function"`. This test makes the fix a property
of the application rather than of the routes somebody remembered to change: it
walks every registered route, recursively through sub-dependencies, and fails if
any of them obtains a session any other way.
"""

from __future__ import annotations

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from threshold.adapters.db import get_session
from threshold.api.main import create_app


def _session_scopes(dependant: Dependant) -> list[str | None]:
    """Every scope under which this dependant, or anything it depends on, takes a session."""
    found: list[str | None] = []
    for dependency in dependant.dependencies:
        if dependency.call is get_session:
            found.append(dependency.scope)
        found.extend(_session_scopes(dependency))
    return found


def _routes() -> list[APIRoute]:
    """Every APIRoute, however this FastAPI version nests them.

    Older FastAPI flattens included routers into ``app.routes``. 0.141 wraps
    each one in an ``_IncludedRouter`` whose ``original_router`` holds the real
    routes, so a flat scan finds nothing -- and the first version of this test
    passed for exactly that reason, checking zero routes and reporting no
    offenders. The vacuity test below exists so that if the shape changes
    again, this goes red instead of quietly becoming a no-op.
    """
    found: list[APIRoute] = []

    def walk(routes: list[object]) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
                continue
            # A wrapped router in 0.141+, or a plain router/mount otherwise.
            inner = getattr(route, "original_router", route)
            nested = getattr(inner, "routes", None)
            if nested is not None:
                walk(list(nested))

    walk(list(create_app().routes))
    return found


def test_every_route_that_takes_a_session_commits_before_responding() -> None:
    offenders = [
        f"{sorted(route.methods)} {route.path}"
        for route in _routes()
        if any(scope != "function" for scope in _session_scopes(route.dependant))
    ]
    assert not offenders, (
        "these routes commit after the response is sent; use SessionDep: " + ", ".join(offenders)
    )


def test_the_check_is_not_vacuous() -> None:
    """If no route took a session at all, the test above would pass by default."""
    with_sessions = [route.path for route in _routes() if _session_scopes(route.dependant)]
    assert len(with_sessions) >= 5, with_sessions
    assert "/v1/match-results/{match_result_id}/decision" in with_sessions
    assert "/v1/documents" in with_sessions
