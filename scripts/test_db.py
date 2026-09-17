"""Create, migrate and seed the database the integration suite owns.

## Why there are two databases

Until now the integration suite ran against the same database as the demo, the
console and a live worker. That is why it was flaky, and the flakiness was not a
collection of unrelated bugs: a test would create a document, the worker would
pick it up and process it, and the test's next assertion would be about a system
that had moved underneath it. Four separate races traced back to that one cause,
and fixing them individually only moved the failure rate down rather than to
zero -- because the next test somebody writes reintroduces it.

Isolation removes the class rather than the instances. Nothing else connects to
`threshold_test`: no worker claims its jobs, no smoke run writes its audit rows,
no demo settles its payments. A test that creates a row is the only thing that
will ever touch that row.

## What still runs against the live database, and why that is right

Six files observe the *running system* rather than the schema -- signing in
through Keycloak, submitting a PDF over HTTP and waiting for a worker to match
it, asking Tempo whether a trace crossed two processes. Pointing those at an
isolated database would not isolate anything; it would only mean asserting about
a database nothing under test is writing to. They keep the live one, and the
races they had are fixed at the source: they now select subjects that cannot be
overtaken rather than whatever happened to be newest.

## Idempotent, and cheap to re-run

`CREATE DATABASE` is skipped when it exists, migrations are `upgrade head`, and
the seed already refuses to double-insert. Running this before every suite costs
about a second and means a developer never has to know it exists.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from urllib.parse import urlsplit

import asyncpg

#: The database the integration suite owns. Overridable so CI can shard, but the
#: default is what `make test-integration` uses and what the tests expect.
TEST_DATABASE_URL = os.environ.get(
    "THRESHOLD_TEST_DATABASE_URL",
    "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold_test",
)

#: Connected to in order to issue `CREATE DATABASE`, which cannot run inside a
#: transaction and cannot run from within the database being created.
MAINTENANCE = "postgres"


def _parts(url: str) -> tuple[str, str]:
    """Split a SQLAlchemy URL into an asyncpg DSN and the database name."""
    split = urlsplit(url.replace("+asyncpg", ""))
    name = split.path.lstrip("/")
    dsn = split._replace(path=f"/{MAINTENANCE}").geturl()
    return dsn, name


async def _create(url: str) -> bool:
    """Create the database unless it is already there. Returns whether it made one."""
    dsn, name = _parts(url)
    connection = await asyncpg.connect(dsn)
    try:
        exists = await connection.fetchval("select 1 from pg_database where datname = $1", name)
        if exists:
            return False
        # The name is not user input -- it comes from a constant in this file or
        # an operator's own environment -- but it still cannot be a parameter,
        # because CREATE DATABASE does not take one. Quoted rather than
        # interpolated bare.
        await connection.execute(f'create database "{name}"')
        return True
    finally:
        await connection.close()


def _run(command: list[str], url: str) -> None:
    """Run a command with the settings pointed at the test database.

    Both alembic and the seed resolve their URL through `get_settings()`, so
    the environment is the whole of the redirection -- there is no second code
    path for tests to drift away from.
    """
    environment = {
        **os.environ,
        "THRESHOLD_DATABASE_URL": url,
        # Required by Settings and irrelevant here: neither migrating nor
        # seeding touches object storage.
        "THRESHOLD_S3_ACCESS_KEY_ID": os.environ.get("THRESHOLD_S3_ACCESS_KEY_ID", "unused"),
        "THRESHOLD_S3_SECRET_ACCESS_KEY": os.environ.get(
            "THRESHOLD_S3_SECRET_ACCESS_KEY", "unused"
        ),
    }
    result = subprocess.run(command, env=environment)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed with {result.returncode}")


def main() -> int:
    url = TEST_DATABASE_URL
    _, name = _parts(url)

    created = asyncio.run(_create(url))
    print(f"{'created' if created else 'found'} database {name}")

    _run([sys.executable, "-m", "alembic", "upgrade", "head"], url)
    _run([sys.executable, "scripts/seed.py"], url)
    print(f"{name} is migrated and seeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
