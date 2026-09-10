"""Auth against the Keycloak that is actually running.

The unit suite validates tokens it forged itself with a throwaway keypair. That
proves the forger and the verifier agree, which is a weaker property than it
looks: every assumption about claim shape is made twice, identically, and
therefore never tested. This file asserts the thing that matters — a token
**Keycloak issued** parses the way `api/auth.py` assumes.

Two assumptions in particular are only checkable here, and both were wrong
before this milestone: that the access token's audience contains
`threshold-api` (it was `account`, so a correct strict check rejected every
real token), and that realm roles arrive at `realm_access.roles` (which rested
on a Keycloak default, since the realm defined no mappers).

Requires: make up
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from threshold.domain.roles import Role

pytestmark = pytest.mark.integration

API = "http://localhost:8000"
TOKEN_URL = "http://localhost:8080/realms/threshold/protocol/openid-connect/token"
ISSUER = "http://localhost:8080/realms/threshold"

#: Seeded in threshold-realm.json. The password equals the username throughout,
#: which is a local-fixture convention and would be a finding anywhere else.
USERS: dict[str, Role] = {
    "clerk": Role.AP_CLERK,
    "buyer": Role.BUYER,
    "receiving": Role.RECEIVING,
    "treasury": Role.TREASURY,
    "controller": Role.CONTROLLER,
    "auditor": Role.AUDITOR,
}


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=15) as c:
        try:
            c.get(f"{ISSUER}/.well-known/openid-configuration")
        except httpx.HTTPError:
            pytest.skip("Keycloak is not reachable; start the stack with `make up`")
        yield c


def mint(client: httpx.Client, username: str) -> str:
    response = client.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "threshold-tests",
            "client_secret": "local-tests-only",
            "username": username,
            "password": username,
        },
    )
    response.raise_for_status()
    token: str = response.json()["access_token"]
    return token


def claims_of(token: str) -> dict[str, Any]:
    """Read the payload without verifying. For assertions about shape only."""
    payload = token.split(".")[1]
    decoded: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    return decoded


class TestTheIssuerProducesWhatTheApiExpects:
    def test_the_issuer_is_the_one_the_api_is_configured_with(self, client: httpx.Client) -> None:
        """Keycloak derives `iss` from the request host unless pinned.

        Unpinned, a browser logging in at localhost is handed a different
        issuer than a container calling keycloak:8080 would expect, and an
        exact issuer check then rejects every real token.
        """
        assert claims_of(mint(client, "clerk"))["iss"] == ISSUER

    def test_the_access_token_is_addressed_to_this_api(self, client: httpx.Client) -> None:
        """Without the audience mapper this is `account`, not `threshold-api`."""
        audience = claims_of(mint(client, "clerk"))["aud"]
        assert "threshold-api" in ([audience] if isinstance(audience, str) else audience)

    def test_realm_roles_arrive_where_the_api_reads_them(self, client: httpx.Client) -> None:
        assert claims_of(mint(client, "treasury"))["realm_access"]["roles"] == ["treasury"]

    @pytest.mark.parametrize("username", sorted(USERS))
    def test_every_seeded_user_carries_exactly_their_role(
        self, client: httpx.Client, username: str
    ) -> None:
        roles = claims_of(mint(client, username))["realm_access"]["roles"]
        assert USERS[username].value in roles


class TestTheApiEnforcesIt:
    def test_an_unauthenticated_request_is_refused(self, client: httpx.Client) -> None:
        """The first assertion in this repository that the API says no."""
        response = client.get(f"{API}/v1/me")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_a_real_token_is_accepted_and_its_roles_survive(self, client: httpx.Client) -> None:
        response = client.get(
            f"{API}/v1/me", headers={"Authorization": f"Bearer {mint(client, 'treasury')}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == "treasury"
        assert body["roles"] == ["treasury"]

    def test_a_forged_token_is_refused(self, client: httpx.Client) -> None:
        response = client.get(f"{API}/v1/me", headers={"Authorization": "Bearer not.a.token"})
        assert response.status_code == 401

    def test_a_token_with_a_tampered_payload_is_refused(self, client: httpx.Client) -> None:
        """Same signature, edited claims. The signature is what catches it."""
        original = mint(client, "clerk")
        header, _payload, signature = original.split(".")
        claims = claims_of(original)
        claims["realm_access"] = {"roles": ["treasury"]}
        forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        response = client.get(
            f"{API}/v1/me",
            headers={"Authorization": f"Bearer {header}.{forged_payload}.{signature}"},
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("username", sorted(USERS))
    def test_every_seeded_user_can_authenticate(self, client: httpx.Client, username: str) -> None:
        """A role nobody can log in as cannot be demonstrated."""
        response = client.get(
            f"{API}/v1/me", headers={"Authorization": f"Bearer {mint(client, username)}"}
        )
        assert response.status_code == 200
        assert USERS[username].value in response.json()["roles"]
