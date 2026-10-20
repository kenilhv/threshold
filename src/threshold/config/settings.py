"""Twelve-factor configuration.

Every value comes from the environment. Nothing has a production-safe default,
because a default that works in production is a secret waiting to be committed —
this codebase has a prior example of exactly that failure and does not repeat it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

from pydantic import PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="THRESHOLD_",
        extra="ignore",
        # A validation error on startup is printed before logging is configured,
        # so the redactor never sees it. Without this the message would carry
        # the raw input, password included.
        hide_input_in_errors=True,
    )

    environment: Literal["local", "ci", "staging", "production"] = "local"
    service_name: str = "threshold-api"
    log_level: str = "INFO"

    database_url: PostgresDsn
    #: Or the parts. RDS and Secrets Manager hand a process a host and a
    #: password, not a DSN, and a URL pasted together in a shell loses a
    #: password containing ``#`` or ``@``. When ``database_url`` is absent and
    #: these are present, the URL is composed here, quoted. An explicit
    #: ``database_url`` always wins; the parts are never merged into one.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "threshold"
    db_user: str | None = None
    db_password: str | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    #: Where each signal is sent, as an OTLP base URL — the SDK appends
    #: ``/v1/traces`` and ``/v1/metrics``.
    #:
    #: Two settings rather than one because they genuinely go to two different
    #: places: spans to Tempo, metrics to Prometheus's OTLP receiver. A single
    #: endpoint was a convenient fiction that held only while both went nowhere,
    #: and collapsing them again would mean either running a collector purely to
    #: fan out two streams on one host, or teaching the app that one of its two
    #: backends lives at a path prefix of the other.
    #:
    #: ``None`` is a supported configuration for either, not a broken one. The
    #: SDK still produces valid spans, so audit rows and log lines still
    #: correlate; instruments stay real and their measurements are dropped.
    #: That is the right default for a unit test, and for a laptop where the
    #: telemetry stack is not running.
    otel_traces_endpoint: str | None = None
    otel_metrics_endpoint: str | None = None

    # S3-compatible. Locally this is MinIO; in AWS only the endpoint and the
    # credential source change, which is the whole point of using boto3 here
    # rather than a MinIO-specific client.
    #
    # The static key pair is optional because in AWS there must not be one: the
    # task role supplies credentials through boto3's default chain, and a key
    # pair baked into a task definition is exactly the credential a task role
    # exists to replace. Locally MinIO needs the pair, and compose sets it.
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket_documents: str = "threshold-documents"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    #: What a token must claim as its issuer, matched exactly.
    oidc_issuer: str | None = None
    #: Where this process can reach the issuer's signing keys. Separate from
    #: the issuer on purpose: the issuer is an identity a token asserts, and
    #: this is a network route. Inside a container network they differ, and
    #: deriving one from the other is what makes an exact issuer check reject
    #: every real token.
    oidc_jwks_url: str | None = None
    oidc_audience: str = "threshold-api"

    @model_validator(mode="before")
    @classmethod
    def _compose_database_url(cls, values: Any) -> Any:
        """Build ``database_url`` from the parts when only the parts were given."""
        if not isinstance(values, dict) or values.get("database_url"):
            return values
        host, user, password = (values.get(k) for k in ("db_host", "db_user", "db_password"))
        if not host or not user or password is None:
            return values
        port = values.get("db_port") or 5432
        name = values.get("db_name") or "threshold"
        composed = dict(values)
        composed["database_url"] = (
            f"postgresql+asyncpg://{quote(str(user), safe='')}:{quote(str(password), safe='')}"
            f"@{host}:{port}/{quote(str(name), safe='')}"
        )
        return composed

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @property
    def is_local(self) -> bool:
        return self.environment in ("local", "ci")


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is read once per process."""
    return Settings()  # type: ignore[call-arg]  # values come from env
