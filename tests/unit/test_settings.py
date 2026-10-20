"""Settings composes a database URL from parts, and no longer demands S3 keys.

Both exist for the AWS shape (infra/aws). RDS and Secrets Manager hand a task a
host and a password rather than a DSN, and an IAM task role supplies S3
credentials that must not also be a static key pair in the environment.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from threshold.config.settings import Settings

DSN_VARS = (
    "THRESHOLD_DATABASE_URL",
    "THRESHOLD_DB_HOST",
    "THRESHOLD_DB_PORT",
    "THRESHOLD_DB_NAME",
    "THRESHOLD_DB_USER",
    "THRESHOLD_DB_PASSWORD",
    "THRESHOLD_S3_ACCESS_KEY_ID",
    "THRESHOLD_S3_SECRET_ACCESS_KEY",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in DSN_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestTheDatabaseUrlIsComposedFromParts:
    def test_the_parts_become_a_quoted_url(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("THRESHOLD_DB_HOST", "db.internal")
        clean_env.setenv("THRESHOLD_DB_USER", "threshold_api")
        clean_env.setenv("THRESHOLD_DB_PASSWORD", "p#ss/w@rd:1")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert str(settings.database_url) == (
            "postgresql+asyncpg://threshold_api:p%23ss%2Fw%40rd%3A1@db.internal:5432/threshold"
        )

    def test_port_and_name_are_honoured(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("THRESHOLD_DB_HOST", "db.internal")
        clean_env.setenv("THRESHOLD_DB_PORT", "6543")
        clean_env.setenv("THRESHOLD_DB_NAME", "ledger")
        clean_env.setenv("THRESHOLD_DB_USER", "threshold")
        clean_env.setenv("THRESHOLD_DB_PASSWORD", "x")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert str(settings.database_url).endswith("@db.internal:6543/ledger")

    def test_an_explicit_url_wins_over_the_parts(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("THRESHOLD_DATABASE_URL", "postgresql+asyncpg://a:b@explicit:5432/one")
        clean_env.setenv("THRESHOLD_DB_HOST", "ignored")
        clean_env.setenv("THRESHOLD_DB_USER", "ignored")
        clean_env.setenv("THRESHOLD_DB_PASSWORD", "ignored")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert str(settings.database_url) == "postgresql+asyncpg://a:b@explicit:5432/one"

    def test_half_the_parts_is_still_no_url(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("THRESHOLD_DB_HOST", "db.internal")
        clean_env.setenv("THRESHOLD_DB_USER", "threshold")
        with pytest.raises(ValidationError, match="database_url"):
            Settings(_env_file=None)  # type: ignore[call-arg]


class TestAlembicNeverSeesThePercentSigns:
    """A quoted password contains ``%XX``, and Alembic's config is a ConfigParser.

    ``set_main_option("sqlalchemy.url", ...)`` with such a URL raises
    ``invalid interpolation syntax`` before any connection is made, with the
    URL in the traceback. env.py therefore hands the URL to SQLAlchemy
    directly; this pins that, because the compose stack can never exercise it.
    """

    def test_env_py_does_not_route_the_url_through_the_config(self) -> None:
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "migrations" / "env.py"
        text = source.read_text(encoding="utf-8")
        assert 'set_main_option("sqlalchemy.url"' not in text
        assert "async_engine_from_config" not in text
        assert "create_async_engine(DATABASE_URL" in text

    def test_a_quoted_password_really_would_break_a_config_parser(self) -> None:
        import configparser

        parser = configparser.ConfigParser()
        parser.add_section("alembic")
        with pytest.raises(ValueError, match="interpolation"):
            parser.set("alembic", "sqlalchemy.url", "postgresql://u:p%23ss@h/d")

    def test_a_validation_error_does_not_echo_the_password(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("THRESHOLD_DB_PASSWORD", "the-secret-value")
        with pytest.raises(ValidationError) as caught:
            Settings(_env_file=None)  # type: ignore[call-arg]
        assert "the-secret-value" not in str(caught.value)


class TestS3CredentialsAreOptional:
    def test_no_key_pair_is_a_valid_configuration(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("THRESHOLD_DATABASE_URL", "postgresql+asyncpg://a:b@h:5432/d")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.s3_access_key_id is None
        assert settings.s3_secret_access_key is None

    def test_the_object_store_builds_without_a_key_pair(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        from threshold.adapters.object_store import S3ObjectStore

        clean_env.setenv("THRESHOLD_DATABASE_URL", "postgresql+asyncpg://a:b@h:5432/d")
        clean_env.setenv("THRESHOLD_S3_REGION", "eu-west-1")
        # boto3 resolves credentials lazily, so constructing the client is the
        # whole of what can be checked here without the network; the point is
        # that `None` reaches boto3 and means "use the default chain".
        store = S3ObjectStore(Settings(_env_file=None))  # type: ignore[call-arg]
        assert store._client.meta.region_name == "eu-west-1"
