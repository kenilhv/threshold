"""What a payment is for cannot change, and neither can where it has been.

Migration 0010 took the superuser away from the application. The review that
followed asked the next question -- what can a compromised application do with
exactly the privileges it was left? -- and answered it by doing it, against a
scratch copy of this schema, as `threshold_app`:

    UPDATE payment
       SET amount_minor = 5000000,
           vendor_bank_account_id = '<another vendor's account>'
     WHERE state = 'pending';                                   -- UPDATE 1

    UPDATE payment SET state = 'pending', settled_at = NULL, rail_attempts = 0
     WHERE state = 'settled';                                   -- UPDATE 1

The first is remittance redirection, after authorisation and before submission,
which is the fraud this system exists to prevent. The second reopens a finished
payment so the executor pays it again. Both used one grant: `UPDATE ON payment`.

That grant was too wide by twelve columns of eighteen. The executor writes six
-- `state`, `submitted_at`, `settled_at`, `rail_attempts`, `rail_reference`,
`failure_reason` -- and has never written the others after the insert. "Rows
with a lifecycle" was the right reason to grant UPDATE and the wrong
granularity: a payment's *state* has a lifecycle, and its amount and payee do
not.

## Three controls, and what each one is for

**Column-level UPDATE**, on `payment`, `job` and `dead_letter`. The application
may change how far along a row is and may not change what the row is. For a job
that means `kind`, `payload` and `dedupe_key` are fixed at insert; for a dead
letter, everything but `replayed_at`.

**`trg_payment_moves_legally`** (BEFORE UPDATE). The transition table from
`domain/payments.py`, a second time, where a process that never imported it
still meets it. `settled` and `failed` are frozen. The identity columns are
compared as well, so the immutability holds for the owner too and can be
asserted by a test that connects as the owner -- the same two-lock shape as
`audit_event`, where the privilege refuses the application and the trigger
refuses everybody.

**`trg_payment_agrees_with_its_authorisation`** (BEFORE INSERT). A payment is
born `pending`, cites an *approving* decision, and carries the amount, currency,
document and vendor of the match result that decision was taken on; its payee is
that vendor's current account. Every one of those is derived by
`PaymentService._create`, and until now a row that skipped the service skipped
all of them.

## What this does not stop, stated here so nobody infers otherwise

A compromised application holds INSERT on `match_result` and
`approval_decision`, because inserting them is its job. It can therefore forge a
consistent chain from the root -- a match result that says auto-approve, the
decision, the payment -- and every check above passes, because every row agrees
with the rows it cites. The database has no independent knowledge of what the
invoice said.

What bounds that attack is older than this file: `vendor_bank_account` is
SELECT-only to the application and the payee is a foreign key into it. The
forged chain can overpay a vendor already on file. It cannot pay a stranger.
D-034 carries the argument.

Revision ID: 0011_payment_integrity
Revises: 0010_least_privilege
Created: 2026-09-17 06:20:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_payment_integrity"
down_revision: str | None = "0010_least_privilege"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "threshold_app"

#: How far along a row is. Everything not listed is what the row *is*.
MUTABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "payment": (
        "state",
        "submitted_at",
        "settled_at",
        "rail_attempts",
        "rail_reference",
        "failure_reason",
    ),
    "job": (
        "state",
        "attempts",
        "run_after",
        "claimed_by",
        "claimed_at",
        "lease_expires_at",
        "last_error",
        "updated_at",
    ),
    "dead_letter": ("replayed_at",),
}

MOVES_LEGALLY = """
CREATE OR REPLACE FUNCTION payment_moves_legally() RETURNS trigger
LANGUAGE plpgsql AS $payment$
BEGIN
    IF OLD.state IN ('settled', 'failed') THEN
        RAISE EXCEPTION 'payment % is % and is not reopened', OLD.id, OLD.state
            USING ERRCODE = 'check_violation';
    END IF;

    IF ROW(NEW.id, NEW.approval_decision_id, NEW.match_result_id, NEW.document_id,
           NEW.vendor_id, NEW.vendor_bank_account_id, NEW.amount_minor, NEW.currency,
           NEW.currency_exponent, NEW.idempotency_key, NEW.rail_id, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.approval_decision_id, OLD.match_result_id, OLD.document_id,
           OLD.vendor_id, OLD.vendor_bank_account_id, OLD.amount_minor, OLD.currency,
           OLD.currency_exponent, OLD.idempotency_key, OLD.rail_id, OLD.created_at)
    THEN
        RAISE EXCEPTION
            'payment %: what it is for, how much and to whom are fixed when it is created',
            OLD.id
            USING ERRCODE = 'check_violation';
    END IF;

    -- domain/payments.py ALLOWED, verbatim. `submitting -> submitting` is the one
    -- self-loop: a rail that did not answer is asked again from the same state.
    IF (OLD.state, NEW.state) NOT IN (
        ('pending',    'submitting'),
        ('submitting', 'submitting'),
        ('submitting', 'submitted'),
        ('submitting', 'failed'),
        ('submitted',  'settled')
    ) THEN
        RAISE EXCEPTION 'payment % cannot move from % to %', OLD.id, OLD.state, NEW.state
            USING ERRCODE = 'check_violation';
    END IF;

    -- How many times money could have moved is a count, and counts do not go down.
    IF NEW.rail_attempts < OLD.rail_attempts THEN
        RAISE EXCEPTION 'payment %: rail_attempts cannot decrease', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END
$payment$;
"""

AGREES = """
CREATE OR REPLACE FUNCTION payment_agrees_with_its_authorisation() RETURNS trigger
LANGUAGE plpgsql AS $payment$
DECLARE
    decided   approval_decision%ROWTYPE;
    matched   match_result%ROWTYPE;
    ordered_from uuid;
    payee     vendor_bank_account%ROWTYPE;
BEGIN
    IF NEW.state <> 'pending' OR NEW.rail_attempts <> 0 OR NEW.rail_reference IS NOT NULL
       OR NEW.submitted_at IS NOT NULL OR NEW.settled_at IS NOT NULL THEN
        RAISE EXCEPTION 'a payment is created pending, with no history of its own'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT * INTO decided FROM approval_decision WHERE id = NEW.approval_decision_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'payment cites approval decision %, which does not exist',
            NEW.approval_decision_id USING ERRCODE = 'foreign_key_violation';
    END IF;
    -- domain/approval.py PAYING, verbatim.
    IF decided.decision NOT IN ('approved', 'auto_approved') THEN
        RAISE EXCEPTION 'approval decision % is %, which does not authorise a payment',
            decided.id, decided.decision USING ERRCODE = 'check_violation';
    END IF;
    IF decided.match_result_id <> NEW.match_result_id THEN
        RAISE EXCEPTION 'payment cites match result % but its authorisation was taken on %',
            NEW.match_result_id, decided.match_result_id USING ERRCODE = 'check_violation';
    END IF;

    SELECT * INTO matched FROM match_result WHERE id = NEW.match_result_id;
    IF matched.document_id <> NEW.document_id THEN
        RAISE EXCEPTION 'payment cites document % but match result % is about %',
            NEW.document_id, matched.id, matched.document_id USING ERRCODE = 'check_violation';
    END IF;
    IF ROW(matched.amount_minor, matched.currency, matched.currency_exponent)
       IS DISTINCT FROM ROW(NEW.amount_minor, NEW.currency, NEW.currency_exponent) THEN
        RAISE EXCEPTION 'payment is for % % but the authorised invoice was for % %',
            NEW.amount_minor, NEW.currency, matched.amount_minor, matched.currency
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT vendor_id INTO ordered_from FROM purchase_order WHERE id = matched.purchase_order_id;
    IF ordered_from IS DISTINCT FROM NEW.vendor_id THEN
        RAISE EXCEPTION 'payment is to vendor % but the order was placed with %',
            NEW.vendor_id, ordered_from USING ERRCODE = 'check_violation';
    END IF;

    SELECT * INTO payee FROM vendor_bank_account WHERE id = NEW.vendor_bank_account_id;
    IF payee.vendor_id IS DISTINCT FROM NEW.vendor_id THEN
        RAISE EXCEPTION 'bank account % does not belong to vendor %',
            NEW.vendor_bank_account_id, NEW.vendor_id USING ERRCODE = 'check_violation';
    END IF;
    IF payee.effective_to IS NOT NULL THEN
        RAISE EXCEPTION 'bank account % was superseded on % and is not paid to',
            payee.id, payee.effective_to USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END
$payment$;
"""


def upgrade() -> None:
    for table, columns in MUTABLE_COLUMNS.items():
        op.execute(f"REVOKE UPDATE ON {table} FROM {ROLE}")
        op.execute(f"GRANT UPDATE ({', '.join(columns)}) ON {table} TO {ROLE}")

    op.execute(MOVES_LEGALLY)
    op.execute(
        "CREATE TRIGGER trg_payment_moves_legally BEFORE UPDATE ON payment "
        "FOR EACH ROW EXECUTE FUNCTION payment_moves_legally()"
    )
    op.execute(AGREES)
    op.execute(
        "CREATE TRIGGER trg_payment_agrees_with_its_authorisation BEFORE INSERT ON payment "
        "FOR EACH ROW EXECUTE FUNCTION payment_agrees_with_its_authorisation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_payment_agrees_with_its_authorisation ON payment")
    op.execute("DROP FUNCTION IF EXISTS payment_agrees_with_its_authorisation()")
    op.execute("DROP TRIGGER IF EXISTS trg_payment_moves_legally ON payment")
    op.execute("DROP FUNCTION IF EXISTS payment_moves_legally()")

    # Back to 0010's table-wide grant. The column grants must go first: a
    # table-level REVOKE removes them too, but stating it keeps this readable.
    for table, columns in MUTABLE_COLUMNS.items():
        op.execute(f"REVOKE UPDATE ({', '.join(columns)}) ON {table} FROM {ROLE}")
        op.execute(f"GRANT UPDATE ON {table} TO {ROLE}")
