"""One document is paid at most once at a time.

A matching job that runs twice paid the invoice twice. Not argued; reproduced,
against the real services, by matching one clean document twice and draining
each approval through the payment executor:

    run 1: match_result v1 auto_approve
    run 2: match_result v2 auto_approve
      v1 -> decision 5fc309e9 (auto_approved)
      v2 -> decision 0935a7ef (auto_approved)
    PAYMENTS FOR ONE INVOICE: 2
      settled 26275 EUR  key=...af17f06060bc
      settled 26275 EUR  key=...530b72ae1018
    invoice is worth 26275; settled total is 52550

A job runs twice when its handler outlives the two-minute lease: the reaper
cannot tell a slow worker from a dead one, hands the job to a second worker, and
until this change both committed.

## Why four guards missed it

Every one of them is keyed on a row the re-run duplicates. `uq_match_result_version`
is `(document_id, version)`, and v2 is simply the next version. The approval
guard is keyed on `match_result_id` and is partial on `approved`/`rejected`, so
`auto_approved` is outside it. `uq_payment_live_per_match` and
`uq_payment_authorisation` are keyed on the match result and the decision, and
both are new. The rail's idempotency key is derived from the decision, so the
rail was asked twice with two keys and correctly honoured both.

Each constraint does exactly what it was built to do. Anchoring more of them to
the match result cannot help, because the match result is the thing that gets
copied. The document is the one row a re-run does not copy.

## The fix is in two places, and this is the second

The first is in the queue and the worker: `succeed()` now reports whether this
worker still held the job, and a worker that lost it raises inside its own
transaction and rolls its work back. That closes the path that was reproduced.

This index is the backstop, for the path nobody has found yet. A replayed dead
letter, a job enqueued by hand during an incident, a future handler that
re-matches for a reason of its own -- any of them can mint a second
authorisation for one invoice, and when one does, the payment executor now
meets Postgres rather than the rail. `PaymentService._create` inserts inside a
savepoint, turns this index's refusal into `PaymentRefusedError` naming the
payment that is already live, and posts no accrual. The handler sends that to
the dead-letter queue, which is to say to a person.

Partial on the live states, `pending` through `settled`, with the same predicate
as `uq_payment_live_per_match` and for the same reason: a failed payment must not
block a corrected one. A document whose payment the rail rejected can be
re-matched and paid; a document with a payment in flight or settled cannot be
paid again without somebody deciding that it should.

## What this does not cover

**A re-rendered duplicate invoice.** The same invoice sent twice -- re-scanned,
re-exported, forwarded by a second person -- is two documents with two ids, and
this index is silent on it. That threat is caught at match time by
`duplicate_suspected`, a blocking exception owned by AP that stops
auto-approval and puts the invoice in front of a person. It compares the invoice
number and vendor name as they were read, so a copy that reads differently on
both gets past it, and past this index too. Nothing here changes that.

**A second authorisation being recorded.** When the backstop fires, the
duplicate match result and decision already exist; this refuses to pay them, it
does not stop them being written. Fewer of them are written now that a losing
worker rolls back, and the ones that remain are visible in the dead-letter queue
instead of in the bank statement.

**A correction to an invoice that is already being paid.** A re-match with a
different amount, while the first payment is live, is refused rather than paid
at the new amount. That is deliberate: two payments for one document is never
the answer to a correction, and the refusal puts the question in front of the
person who can answer it.

## What was rejected

**Supervising the handler instead.** Heartbeat from a sibling task and cancel the
handler when the heartbeat comes back False, so a slow worker stops rather than
finishing. It does not work here. The handler and its supervisor would share one
`AsyncSession`, which is not safe to cancel mid-statement. `CancelledError` is a
`BaseException`, so `session_scope`'s `except Exception` would not roll back.
And `.cancel()` cannot interrupt the synchronous pypdf parse, which is the one
handler slow enough to outlive a lease. A slow handler therefore still loses its
lease; what changed is that its work is rolled back and redone by the other
worker, not duplicated.

**A full unique index on `payment(document_id)`.** Simpler, and wrong: it would
make a rejected payment permanent, since the corrected one could never be
inserted.

**Tidying existing violations.** If rows already break this rule the migration
refuses and names them, as 0012 did for ledger transactions that cited nothing.
Two live payments for one document are an invoice paid twice: money to recover
from a supplier and a decision about which payment stands. Neither is a
migration's to make, and deleting or failing one here would erase the evidence
that it happened. (The first database this met, it refused: the integration
database still held two settled 26275 EUR payments on one fixture document,
written two minutes before the commit that recorded the reproduction above.)

**`CREATE INDEX CONCURRENTLY`.** Alembic runs this upgrade in one transaction
(`transaction_per_migration=False` in `env.py`), and a concurrent build cannot
run inside one. The plain build holds a SHARE lock on `payment` -- reads carry
on, the executor's writes wait -- for as long as it takes to index a table that
holds one row per invoice paid.

Revision ID: 0014_payment_per_document
Revises: 0013_audit_clock
Created: 2026-09-30 10:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_payment_per_document"
down_revision: str | None = "0013_audit_clock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_payment_live_per_document"

#: The same predicate as `uq_payment_live_per_match`, character for character.
LIVE = "state IN ('pending', 'submitting', 'submitted', 'settled')"

#: One statement: asyncpg prepares, and a prepared statement may not carry two.
PREFLIGHT = """
DO $preflight$
DECLARE
    doubled text;
BEGIN
    SELECT string_agg(document_id::text || ' (' || payments || ')', '; ' ORDER BY document_id)
      INTO doubled
      FROM (
          SELECT document_id,
                 string_agg(
                     id::text || ' ' || state || ' ' || amount_minor || ' ' || currency,
                     ', ' ORDER BY created_at
                 ) AS payments
            FROM payment
           WHERE state IN ('pending', 'submitting', 'submitted', 'settled')
           GROUP BY document_id
          HAVING count(*) > 1
      ) AS twice;
    IF doubled IS NOT NULL THEN
        RAISE EXCEPTION
            'these documents have more than one live payment: %. Each is an invoice '
            'paid, or being paid, more than once -- money to recover from a supplier, '
            'not rows to tidy. Reconcile them, remove them only if they are known test '
            'residue, and run this migration again. It will not decide which payment '
            'was the real one.', doubled
            USING ERRCODE = 'unique_violation';
    END IF;
END
$preflight$;
"""


def upgrade() -> None:
    op.execute(PREFLIGHT)
    op.create_index(
        INDEX,
        "payment",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="payment", postgresql_where=sa.text(LIVE))
