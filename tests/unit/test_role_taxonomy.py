"""The realm, the Role enum and the exception taxonomy must agree.

Nothing checked this before, and all three had drifted. `domain/taxonomy.py`
routed exceptions to `receiving` and `treasury`; neither was a realm role, so
the two owners doing the most consequential work in the system — goods receipt
and payment instructions — could not be held by anybody. No seeded user held
`buyer`. The Keycloak README asserted that `controller` was the sole approver of
vendor banking changes, which the taxonomy contradicts.

None of it had bitten, because no endpoint was authenticated. That is exactly
the condition under which this class of drift survives: the configuration is
inert, so nothing disagrees with it out loud.

Reads the committed realm JSON from disk. No Keycloak, no database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from threshold.domain.roles import READ_ONLY_ROLES, Role, role_for_owner
from threshold.domain.taxonomy import TAXONOMY, OwnerRole

REALM_PATH = Path(__file__).resolve().parents[2] / "infra" / "local" / "keycloak"
REALM_FILE = REALM_PATH / "threshold-realm.json"


@pytest.fixture(scope="module")
def realm() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(REALM_FILE.read_text(encoding="utf-8"))
    return payload


def _realm_role_names(realm: dict[str, Any]) -> set[str]:
    return {role["name"] for role in realm["roles"]["realm"]}


class TestTheEnumCoversTheTaxonomy:
    def test_every_exception_owner_is_a_role_a_person_can_hold(self) -> None:
        """The drift that started this file."""
        for owner in OwnerRole:
            assert owner.value in {r.value for r in Role}, (
                f"{owner.value} owns exceptions but is not a role anyone can hold"
            )

    def test_role_for_owner_is_total(self) -> None:
        for owner in OwnerRole:
            assert role_for_owner(owner).value == owner.value

    def test_the_read_only_role_owns_no_exceptions(self) -> None:
        """An auditor who could be assigned work would not be read-only."""
        owning = {role_for_owner(spec.owner) for spec in TAXONOMY.values()}
        assert not (owning & READ_ONLY_ROLES)


class TestTheRealmMatchesTheEnum:
    def test_every_role_exists_in_the_realm(self, realm: dict[str, Any]) -> None:
        missing = {r.value for r in Role} - _realm_role_names(realm)
        assert not missing, f"roles the code knows and Keycloak does not: {sorted(missing)}"

    def test_the_realm_defines_no_roles_the_code_does_not_know(self, realm: dict[str, Any]) -> None:
        """A role only Keycloak knows about is an authorisation surface outside this repo."""
        extra = _realm_role_names(realm) - {r.value for r in Role}
        assert not extra, f"realm roles with no meaning in the code: {sorted(extra)}"

    def test_every_role_is_held_by_a_seeded_user(self, realm: dict[str, Any]) -> None:
        """A role nobody holds cannot be demonstrated, so it cannot be trusted."""
        held: set[str] = set()
        for user in realm["users"]:
            held.update(user.get("realmRoles", []))
        unheld = {r.value for r in Role} - held
        assert not unheld, f"roles no seeded user holds: {sorted(unheld)}"


class TestTheRealmDeliversWhatTheApiParses:
    """The realm previously defined no protocol mappers at all.

    Role delivery then rests on a Keycloak default, and the access token's
    audience is `account` — so a correct, strict audience check rejects every
    real token. Both are pinned here rather than left to a default that a major
    version can change underneath the project.
    """

    @pytest.mark.parametrize("client_id", ["threshold-console", "threshold-tests"])
    def test_the_token_carries_this_api_in_its_audience(
        self, realm: dict[str, Any], client_id: str
    ) -> None:
        client = next(c for c in realm["clients"] if c["clientId"] == client_id)
        mappers = client.get("protocolMappers", [])
        audience = [m for m in mappers if m["protocolMapper"] == "oidc-audience-mapper"]
        assert audience, f"{client_id} has no audience mapper"
        assert audience[0]["config"]["included.client.audience"] == "threshold-api"
        assert audience[0]["config"]["access.token.claim"] == "true"

    @pytest.mark.parametrize("client_id", ["threshold-console", "threshold-tests"])
    def test_the_token_carries_realm_roles_where_the_api_looks(
        self, realm: dict[str, Any], client_id: str
    ) -> None:
        client = next(c for c in realm["clients"] if c["clientId"] == client_id)
        mappers = client.get("protocolMappers", [])
        roles = [m for m in mappers if m["protocolMapper"] == "oidc-usermodel-realm-role-mapper"]
        assert roles, f"{client_id} has no realm-role mapper"
        # api/auth.py reads claims["realm_access"]["roles"].
        assert roles[0]["config"]["claim.name"] == "realm_access.roles"
        assert roles[0]["config"]["multivalued"] == "true"
        assert roles[0]["config"]["access.token.claim"] == "true"


class TestTheBrowserClientStaysABrowserClient:
    def test_the_console_does_not_enable_direct_access_grants(self, realm: dict[str, Any]) -> None:
        """Password grant from a public client is the flow PKCE exists to replace."""
        console = next(c for c in realm["clients"] if c["clientId"] == "threshold-console")
        assert console["directAccessGrantsEnabled"] is False
        assert console["publicClient"] is True
        assert console["attributes"]["pkce.code.challenge.method"] == "S256"

    def test_the_test_client_is_confidential_and_says_why_it_exists(
        self, realm: dict[str, Any]
    ) -> None:
        harness = next(c for c in realm["clients"] if c["clientId"] == "threshold-tests")
        assert harness["directAccessGrantsEnabled"] is True
        assert harness["publicClient"] is False
        assert "local" in harness["description"].lower()

    def test_the_api_client_accepts_no_flows(self, realm: dict[str, Any]) -> None:
        """It validates tokens; it never mints them."""
        api = next(c for c in realm["clients"] if c["clientId"] == "threshold-api")
        assert api["bearerOnly"] is True
        assert api["standardFlowEnabled"] is False
        assert api["serviceAccountsEnabled"] is False


class TestTheDocumentationMatchesTheRealm:
    def test_the_role_table_names_every_role(self) -> None:
        """The README's table had been contradicting the taxonomy."""
        readme = (REALM_PATH / "README.md").read_text(encoding="utf-8")
        for role in Role:
            assert f"`{role.value}`" in readme, f"{role.value} is undocumented"

    def test_no_document_still_claims_a_delegation_limit(self) -> None:
        """No such limit exists in the code. It was a specification nobody wrote."""
        for path in (REALM_FILE, REALM_PATH / "README.md"):
            assert "delegation limit" not in path.read_text(encoding="utf-8").lower(), (
                f"{path.name} still describes a control that does not exist"
            )
