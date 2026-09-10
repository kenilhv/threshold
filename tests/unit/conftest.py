"""Settings the unit suite needs in order to import API modules at all.

`api/main.py` builds the app at module scope — `app = create_app()` — because
that is the object uvicorn is pointed at. The consequence is that importing
anything from that module reads `Settings`, and `Settings` has four required
fields with no defaults, so a unit test that never touches a database still
fails at collection without them.

These values are deliberately unreachable rather than plausible. Nothing in the
unit suite may open a connection, and a hostname that does not resolve turns a
test that quietly starts talking to a real service into a test that fails
loudly. `make test` claims to need no stack; this is part of what makes that
true.
"""

from __future__ import annotations

import os

_UNREACHABLE: dict[str, str] = {
    "THRESHOLD_ENVIRONMENT": "local",
    "THRESHOLD_DATABASE_URL": "postgresql+asyncpg://unit:unit@unit-tests.invalid:5432/unit",
    "THRESHOLD_REDIS_URL": "redis://unit-tests.invalid:6379/0",
    "THRESHOLD_S3_ENDPOINT_URL": "http://unit-tests.invalid:9000",
    "THRESHOLD_S3_ACCESS_KEY_ID": "unit",
    "THRESHOLD_S3_SECRET_ACCESS_KEY": "unit",
    "THRESHOLD_OIDC_ISSUER": "http://unit-tests.invalid/realms/threshold",
    "THRESHOLD_OIDC_JWKS_URL": "http://unit-tests.invalid/certs",
}

# setdefault, not assignment: a developer running the unit suite against a live
# stack on purpose should not be silently overridden.
for key, value in _UNREACHABLE.items():
    os.environ.setdefault(key, value)
