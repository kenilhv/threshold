"""The two databases this suite talks to, and which one a test wants.

Every integration test used to point at one URL: the database the demo, the
console and a live worker all share. That is why the suite was flaky, and the
flakiness was one cause wearing four costumes -- a test would write a document,
the worker would pick it up and process it, and the test's next assertion would
be about a system that had moved underneath it.

Fixing those four races individually moved the failure rate down and not to
zero, because the fifth is whatever somebody writes next. The split here removes
the class instead of the instances.

## `ISOLATED` -- nothing else is connected

`scripts/test_db.py` creates, migrates and seeds it. No worker claims its jobs,
no smoke run writes its audit rows, no demo settles its payments. A test that
creates a row is the only thing that will ever touch that row, so "order by
decided_at desc" means what it says and a job stays enqueued until the test
runs it.

Used by every file that reaches the schema directly.

## `LIVE` -- the database the running system is using

Some tests are *about* the running system: signing in through Keycloak,
submitting a PDF over HTTP and waiting for a worker to match it, asking Tempo
whether one trace crossed two processes. Pointing those at an isolated database
would not isolate them; it would mean asserting against a database that nothing
under test writes to.

They keep the live one, and the races they had are fixed at the source -- by
selecting a subject that cannot be overtaken rather than whichever row happened
to be newest.

A test that reads this constant is making a claim about the deployed system.
That is a bigger claim than reading `ISOLATED`, which is why they have different
names rather than one configurable value.
"""

from __future__ import annotations

import os
from typing import Final

#: Owned by the integration suite. Nothing else connects to it.
ISOLATED: Final = os.environ.get(
    "THRESHOLD_TEST_DATABASE_URL",
    "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold_test",
)

#: What the API, the worker and the console are actually using. Read only by
#: tests whose subject is the running system.
LIVE: Final = os.environ.get(
    "THRESHOLD_DATABASE_URL",
    "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold",
)
