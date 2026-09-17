"""Two audit events in one transaction can be told apart.

`audit_event.occurred_at` defaulted to `now()`, which in Postgres is the
*transaction's* start time and does not advance inside it. Every event written
in one transaction therefore carried a byte-identical timestamp, and the demo
database holds 2,621 groups of rows that cannot be ordered against each other.

That is not a cosmetic problem for this table. The audit log exists to answer
"who approved this and what did the system know", and a matched invoice writes
`match.decided` and `invoice.auto_approved` in the same transaction. The order
between them is the causality -- the decision is recorded because the match
cleared -- and `ORDER BY occurred_at` was returning them in whichever order the
rows happened to sit on disk.

`test_trace_continuity.py` asserts that story reads in order. It has been
passing on physical row layout since M5 and flipped when an unrelated change
altered it, which is how this was found: a test that was right about the
property and lucky about the evidence.

`clock_timestamp()` reads the wall clock at each call, so rows written in one
transaction are ordered by when they were written. The cost is that timestamps
within a transaction are no longer identical, which was never a property
anybody wanted.

Existing rows keep the timestamps they have. Back-dating them would be
inventing an order nobody recorded, in the one table whose whole value is that
it records what happened.

Revision ID: 0013_audit_clock
Revises: 0012_two_roles
Created: 2026-09-17 09:40:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_audit_clock"
down_revision: str | None = "0012_two_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE audit_event ALTER COLUMN occurred_at SET DEFAULT clock_timestamp()")


def downgrade() -> None:
    op.execute("ALTER TABLE audit_event ALTER COLUMN occurred_at SET DEFAULT now()")
