"""Pay three schema debts: a dead column, a missing index, an unwritten one.

Three unrelated changes in one revision because they are one piece of work --
the M8 debt sweep -- and splitting them would make the history harder to read
rather than easier.

## `match_exception.resolved_at` goes

Declared at M3, read by two queries, written by nothing. Both readers spell
`WHERE resolved_at IS NULL`, which has been universally true for every row ever
written, so the predicate has never excluded anything: you could delete both
clauses today and every test would still pass.

Removing rather than populating, and the argument is not laziness. The system
already answers "is this exception still open" correctly and in one place --
the queue excludes an invoice once a terminal `approval_decision` exists
against its match result. A second, half-implemented mechanism for the same
question is worse than none, because the day somebody starts writing
`resolved_at` the two answers can disagree and only one of them is tested.

`ix_match_exception_open_by_owner` goes with it. It was a partial index on
`(owner_role, due_at) WHERE resolved_at IS NULL` -- a role-scoped worklist,
which would be exactly right if anything queried by `owner_role`. Nothing does.
It has been indexing every exception ever raised, 3,264 of them at the time of
writing, in service of no query.

## `ix_match_exception_match_result` arrives

The index that is actually needed, and the only item in this revision with a
cost today. The queue endpoint runs one lookup per row --
`WHERE match_result_id = ? ORDER BY due_at LIMIT 1` -- and `match_exception`
carried no index on `match_result_id` at all. Fifty rows on a page meant fifty
sequential scans of a growing table.

`ApprovalService.open_exceptions` has the same shape and runs once per queue
row and again on every decision.

## `dead_letter.dedupe_key` gets its history back

The column has existed since M2 and nothing ever wrote it, which the queue's own
module docstring cited as proof that hand-copied columns get missed. It is
written now, and the rows that died before this migration are recoverable:
dead-lettering does not delete the job (D-007), so the original row survives in
`state='dead'` with its key intact. One UPDATE joins them back.

Revision ID: 0009_debts
Revises: 0008_ledger_trigger_both_sides
Created: 2026-09-17 02:40:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_debts"
down_revision: str | None = "0008_ledger_trigger_both_sides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Backfill before anything else: it reads `job`, which nothing here touches,
    # and doing it first means a failure later leaves the data already correct.
    op.execute(
        """
        UPDATE dead_letter AS d
           SET dedupe_key = j.dedupe_key
          FROM job AS j
         WHERE j.id = d.original_job_id
           AND d.dedupe_key IS NULL
           AND j.dedupe_key IS NOT NULL
        """
    )

    op.create_index(
        "ix_match_exception_match_result",
        "match_exception",
        ["match_result_id", "due_at"],
        unique=False,
    )

    op.drop_index("ix_match_exception_open_by_owner", table_name="match_exception")
    op.drop_column("match_exception", "resolved_at")


def downgrade() -> None:
    # The column comes back nullable and empty, which is exactly the state it
    # was in: nothing ever wrote it, so there is nothing to restore.
    op.add_column(
        "match_exception",
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_match_exception_open_by_owner",
        "match_exception",
        ["owner_role", "due_at"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.drop_index("ix_match_exception_match_result", table_name="match_exception")

    # The backfill is deliberately not reversed. Setting the column back to NULL
    # would destroy correct data to restore an absence, and an absence is not a
    # state anything depends on.
