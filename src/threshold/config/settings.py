"""Twelve-factor configuration.

Every value comes from the environment. Nothing has a production-safe default,
because a default that works in production is a secret waiting to be committed —
this codebase has a prior example of exactly that failure and does not repeat it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="THRESHOLD_",
        extra="ignore",
    )

    environment: Literal["local", "ci", "staging", "production"] = "local"
    service_name: str = "threshold-api"
    log_level: str = "INFO"

    database_url: PostgresDsn
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    redis_url: RedisDsn

    #: Where traces and metrics are sent. None is a supported configuration,
    #: not a broken one: the SDK still produces valid spans, so audit rows and
    #: log lines still correlate, and nothing is exported. That is the right
    #: default for a unit test and for a laptop with no collector running.
    otel_exporter_otlp_endpoint: str | None = None

    # S3-compatible. Locally this is MinIO; in AWS only the endpoint and the
    # credential source change, which is the whole point of using boto3 here
    # rather than a MinIO-specific client.
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket_documents: str = "threshold-documents"
    s3_access_key_id: str
    s3_secret_access_key: str

    #: What a token must claim as its issuer, matched exactly.
    oidc_issuer: str | None = None
    #: Where this process can reach the issuer's signing keys. Separate from
    #: the issuer on purpose: the issuer is an identity a token asserts, and
    #: this is a network route. Inside a container network they differ, and
    #: deriving one from the other is what makes an exact issuer check reject
    #: every real token.
    oidc_jwks_url: str | None = None
    oidc_audience: str = "threshold-api"

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
