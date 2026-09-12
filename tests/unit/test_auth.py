"""Token validation, including the ways it is usually got wrong.

An RSA keypair is generated per run and served as an in-memory JWKS, so these
need no Keycloak and no network. That the *real* issuer's tokens parse the way
this assumes is a separate question, and it is answered in
``tests/integration/test_auth.py`` against a token minted by the running
Keycloak — because an auth test that only ever validates tokens it forged
itself proves the forger and the verifier agree, which is not the property
anyone cares about.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.utils import base64url_encode

from threshold.api.auth import Principal, jwks_cache, require_principal
from threshold.config.settings import Settings, get_settings
from threshold.domain.roles import Role

ISSUER = "http://localhost:8080/realms/threshold"
AUDIENCE = "threshold-api"
KID = "test-key-1"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_document() -> dict[str, Any]:
    numbers = _PRIVATE_KEY.public_key().public_numbers()

    def b64(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return base64url_encode(value.to_bytes(length, "big")).decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "use": "sig",
                "alg": "RS256",
                "n": b64(numbers.n),
                "e": b64(numbers.e),
            }
        ]
    }


def _pem() -> str:
    return _PRIVATE_KEY.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def token(
    *,
    issuer: str = ISSUER,
    audience: str | list[str] = AUDIENCE,
    roles: list[str] | None = None,
    expires_in: int = 900,
    not_before: int = 0,
    kid: str | None = KID,
    subject: str = "user-1",
    include: dict[str, Any] | None = None,
    omit: tuple[str, ...] = (),
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "nbf": int((now + timedelta(seconds=not_before)).timestamp()),
        "preferred_username": "ada",
        "realm_access": {"roles": roles if roles is not None else ["ap_clerk"]},
    }
    claims.update(include or {})
    for key in omit:
        claims.pop(key, None)
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, _pem(), algorithm="RS256", headers=headers)


@pytest.fixture(autouse=True)
def _serve_keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Serve the fixture JWKS and record every fetch."""
    fetches: list[str] = []

    def fake_fetch(url: str) -> dict[str, Any]:
        fetches.append(url)
        return _jwks_document()

    monkeypatch.setattr("threshold.api.auth._fetch", fake_fetch)
    jwks_cache().clear()
    yield fetches
    jwks_cache().clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A minimal app whose only route is guarded by the real dependency."""
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
        return {
            "subject": principal.subject,
            "username": principal.username,
            "roles": sorted(r.value for r in principal.roles),
        }

    app.dependency_overrides[get_settings] = lambda: _settings(
        issuer=ISSUER, jwks_url="http://keycloak:8080/certs"
    )
    with TestClient(app) as c:
        yield c


def _settings(*, issuer: str | None, jwks_url: str | None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",  # type: ignore[arg-type]
        s3_access_key_id="x",
        s3_secret_access_key="x",
        oidc_issuer=issuer,
        oidc_jwks_url=jwks_url,
        oidc_audience=AUDIENCE,
    )


class TestAccepts:
    def test_a_well_formed_token(self, client: TestClient) -> None:
        response = client.get("/whoami", headers={"Authorization": f"Bearer {token()}"})
        assert response.status_code == 200
        assert response.json()["roles"] == ["ap_clerk"]
        assert response.json()["username"] == "ada"

    def test_an_audience_list_containing_this_api(self, client: TestClient) -> None:
        """Keycloak issues `aud` as a list once more than one mapper contributes."""
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token(audience=['account', AUDIENCE])}"},
        )
        assert response.status_code == 200

    def test_unknown_roles_are_dropped_not_carried(self, client: TestClient) -> None:
        """A role defined in Keycloak that this system has no concept of.

        Carrying it would put the authorisation surface in an admin console
        rather than in this repository.
        """
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token(roles=['ap_clerk', 'realm-admin'])}"},
        )
        assert response.status_code == 200
        assert response.json()["roles"] == ["ap_clerk"]

    def test_every_role_the_realm_defines_is_understood(self, client: TestClient) -> None:
        response = client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {token(roles=[r.value for r in Role])}"},
        )
        assert response.status_code == 200
        assert response.json()["roles"] == sorted(r.value for r in Role)


class TestRefuses:
    def test_no_token(self, client: TestClient) -> None:
        response = client.get("/whoami")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_a_token_that_is_not_a_jwt(self, client: TestClient) -> None:
        assert (
            client.get("/whoami", headers={"Authorization": "Bearer nonsense"}).status_code == 401
        )

    def test_an_expired_token(self, client: TestClient) -> None:
        bad = token(expires_in=-3600)
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_a_token_not_yet_valid(self, client: TestClient) -> None:
        bad = token(not_before=3600)
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_a_token_from_another_issuer(self, client: TestClient) -> None:
        """Correctly signed, genuinely issued, and not for this realm."""
        bad = token(issuer="http://localhost:8080/realms/somewhere-else")
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_a_token_for_another_audience(self, client: TestClient) -> None:
        """Without this check, any token any client in the realm holds works here."""
        bad = token(audience="some-other-service")
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_a_token_signed_with_none(self, client: TestClient) -> None:
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1"}
        header = base64url_encode(json.dumps({"alg": "none", "kid": KID}).encode()).decode()
        body = base64url_encode(json.dumps(claims).encode()).decode()
        bad = f"{header}.{body}."
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_a_token_signed_hs256_with_the_public_key_as_the_secret(
        self, client: TestClient
    ) -> None:
        """The classic algorithm-confusion attack.

        The public key is public. A verifier that reads `alg` out of the token
        will treat that key as an HMAC secret and accept a token the attacker
        signed themselves. Passing `algorithms` explicitly is what makes this a
        401 rather than a treasury-role principal.

        Assembled by hand because PyJWT refuses to sign with a PEM public key --
        a defence at the library level, which is welcome and also means the
        attack cannot be written through its encoder.
        """
        public_pem = _PRIVATE_KEY.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        now = datetime.now(UTC)
        header = base64url_encode(json.dumps({"alg": "HS256", "kid": KID}).encode())
        payload = base64url_encode(
            json.dumps(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "sub": "attacker",
                    "iat": int(now.timestamp()),
                    "exp": int((now + timedelta(seconds=900)).timestamp()),
                    "realm_access": {"roles": ["treasury"]},
                }
            ).encode()
        )
        signing_input = header + b"." + payload
        signature = base64url_encode(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
        bad = (signing_input + b"." + signature).decode()

        response = client.get("/whoami", headers={"Authorization": f"Bearer {bad}"})
        assert response.status_code == 401, "algorithm confusion was accepted"

    def test_a_token_signed_by_a_different_key(self, client: TestClient) -> None:
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        now = datetime.now(UTC)
        bad = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "attacker",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=900)).timestamp()),
            },
            pem,
            algorithm="RS256",
            headers={"kid": KID},
        )
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_a_token_with_no_key_id(self, client: TestClient) -> None:
        bad = token(kid=None)
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401

    def test_a_token_missing_a_required_claim(self, client: TestClient) -> None:
        bad = token(omit=("exp",))
        assert client.get("/whoami", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


class TestKeyFetching:
    def test_keys_are_fetched_once_and_reused(
        self, client: TestClient, _serve_keys: list[str]
    ) -> None:
        for _ in range(5):
            assert (
                client.get("/whoami", headers={"Authorization": f"Bearer {token()}"}).status_code
                == 200
            )
        assert len(_serve_keys) == 1, f"fetched {len(_serve_keys)} times, should cache"

    def test_an_unknown_key_id_does_not_refetch_per_request(
        self, client: TestClient, _serve_keys: list[str]
    ) -> None:
        """Otherwise anyone can drive load at the issuer with invented key ids."""
        client.get("/whoami", headers={"Authorization": f"Bearer {token()}"})
        assert len(_serve_keys) == 1

        for _ in range(10):
            response = client.get(
                "/whoami", headers={"Authorization": f"Bearer {token(kid='made-up')}"}
            )
            assert response.status_code == 401
        assert len(_serve_keys) == 1, "an unknown kid triggered a fetch storm"

    def test_unreachable_keys_are_a_503_not_a_pass(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The direction this fails in is the whole point."""

        def explode(url: str) -> dict[str, Any]:
            raise OSError("connection refused")

        monkeypatch.setattr("threshold.api.auth._fetch", explode)
        jwks_cache().clear()
        response = client.get("/whoami", headers={"Authorization": f"Bearer {token()}"})
        assert response.status_code == 503


class TestConfiguration:
    def test_an_unconfigured_issuer_refuses_rather_than_waves_through(self) -> None:
        """A deployment that forgot to set an issuer must not become an open API."""
        app = FastAPI()

        @app.get("/whoami")
        async def whoami(principal: Principal = Depends(require_principal)) -> dict[str, str]:
            return {"subject": principal.subject}

        app.dependency_overrides[get_settings] = lambda: _settings(issuer=None, jwks_url=None)
        with TestClient(app) as c:
            assert c.get("/whoami", headers={"Authorization": "Bearer x"}).status_code == 503
