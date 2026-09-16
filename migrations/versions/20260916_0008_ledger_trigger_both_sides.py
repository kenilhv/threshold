"""The balance trigger checks both sides of an UPDATE.

0007 shipped a function that resolved the transaction to check as
``COALESCE(NEW.transaction_id, OLD.transaction_id)``. That is correct for INSERT
(NEW is set) and for DELETE (NEW is null, OLD is set), and wrong for UPDATE,
where NEW is always set and OLD is therefore never looked at.

The hole it leaves is not theoretical. An UPDATE that moves a posting from one
transaction to another unbalances *both*, and the destination check normally
catches it -- gaining a non-zero entry unbalances a transaction that was
balanced. But a transaction built unbalanced inside the same statement and then
balanced by stealing a row from a settled one leaves the destination balanced
and the source not. Reproduced against this database before this migration was
written: the COMMIT succeeded and left a committed transaction summing to 700.

The fix checks the distinct set of transactions the row touched, which for an
UPDATE is two. `TG_OP` is used rather than a null check because a row can
legitimately be moved to or from anywhere, and inferring the operation from
which of NEW and OLD is populated is the inference that caused this.

Revision ID: 0008_ledger_trigger_both_sides
Revises: 0007_payments
Created: 2026-09-16 22:20:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_ledger_trigger_both_sides"
down_revision: str | None = "0007_payments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CORRECT = """
CREATE OR REPLACE FUNCTION ledger_entry_balanced() RETURNS trigger
LANGUAGE plpgsql AS $ledger$
DECLARE
    subject uuid;
    total bigint;
BEGIN
    FOR subject IN
        SELECT DISTINCT candidate
          FROM unnest(ARRAY[
                   CASE WHEN TG_OP <> 'INSERT' THEN OLD.transaction_id END,
                   CASE WHEN TG_OP <> 'DELETE' THEN NEW.transaction_id END
               ]) AS candidate
         WHERE candidate IS NOT NULL
    LOOP
        -- COALESCE, not a bare sum: deleting every posting of a transaction is
        -- legal, and an empty set sums to NULL rather than to zero.
        SELECT COALESCE(sum(amount_minor), 0) INTO total
            FROM ledger_entry WHERE transaction_id = subject;
        IF total <> 0 THEN
            RAISE EXCEPTION
                'ledger transaction % does not balance: entries sum to %', subject, total
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NULL;
END
$ledger$;
"""

ORIGINAL = """
CREATE OR REPLACE FUNCTION ledger_entry_balanced() RETURNS trigger
LANGUAGE plpgsql AS $ledger$
DECLARE
    subject uuid;
    total bigint;
BEGIN
    subject := COALESCE(NEW.transaction_id, OLD.transaction_id);
    SELECT COALESCE(sum(amount_minor), 0) INTO total
        FROM ledger_entry WHERE transaction_id = subject;
    IF total <> 0 THEN
        RAISE EXCEPTION
            'ledger transaction % does not balance: entries sum to %', subject, total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$ledger$;
"""


def upgrade() -> None:
    # The trigger itself is unchanged and stays attached: only the function body
    # is replaced, so there is no window in which the table is unguarded.
    op.execute(CORRECT)


def downgrade() -> None:
    op.execute(ORIGINAL)
