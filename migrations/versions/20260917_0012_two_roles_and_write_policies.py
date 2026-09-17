"""Two runtime roles, and rules about which of them may write what.

0010 took the superuser away and 0011 made a payment's identity immutable. D-034
then said plainly what was left: an application holding INSERT on
`match_result` and `approval_decision` can forge a consistent chain from the
root, and the reviewers did, to `settled`.

That paragraph treated "the application" as one thing, and it is two.

The **worker** parses documents sent by strangers. It runs a PDF library over
hostile bytes, and of everything in this system it is the process most likely to
be taken over. It has no business recording that a *person* approved something.

The **API** authenticates people and records what they decide. It never extracts,
matches, pays or posts, and -- traced through the code before this was written --
it never issues an UPDATE or a DELETE at all.

So they get a role each, and the rules below are keyed on `current_user`, which
is fixed by the credential a process was started with and is not something it
can change from SQL. (0010 verified that `SET ROLE` and `SET SESSION
AUTHORIZATION` are refused.)

## What each role may write

    threshold_api      INSERT  document, idempotency_key, job, audit_event,
                               approval_decision  (actor_type = 'human' only)
                       no UPDATE, no DELETE, anywhere

    threshold_worker   INSERT  extraction, match_result, match_exception,
                               escalation, payment, ledger_*, job, dead_letter,
                               audit_event,
                               approval_decision  (system, 'auto_approved', and
                               only on a match result that says auto_approve)
                       UPDATE  the 0011 columns; DELETE job

A compromised worker can no longer write a human's decision, so it cannot
release an invoice the match blocked: blocked invoices are released by people,
and that row is one it may not write. A compromised API cannot write a match
result, a payment or a ledger entry, so it cannot forge from the root.

Neither is harmless. The worker decides what auto-approves, so it can still
auto-approve a fabricated invoice -- to a vendor already on file, as before. The
API is the process that knows who a person is, so it can still say a person
approved. Those are the two trust decisions this design actually makes, and
D-035 names them.

## The second policy, and what it is honestly for

`may_decide()` is `domain/approval.py::authorise()` in SQL: the people recorded
in `threshold.actor_roles` must between them own every exception on the match
result, and none of them may be read-only. The API sets that value with
`set_config(..., true)` -- transaction-local, so it cannot leak across a pooled
connection -- immediately before it inserts.

A compromised API can set it to anything. This policy is therefore **not** a
defence against a compromised API, and nothing here says it is. It is a defence
against the API's own bugs: a route added next year that inserts a decision
without calling `authorise()` is refused by Postgres. Fail-closed by
construction -- an absent setting reads as `''` on a recycled connection rather
than NULL, hence `nullif`, and a NULL role list permits nothing.

## The ledger cites what it moves

The review posted a balanced transaction that referred to nothing, and another
with an invented kind, an invented account and an invented currency; both
committed. "Balanced" is not "true". So:

- kinds and accounts are closed vocabularies, checked;
- a transaction cites exactly one payment or match result, unless it is an
  `opening` balance -- which the worker may not post;
- the deferred trigger that checks the sum now also checks that the amount moved
  is the amount on the row it cites, in that row's currency;
- a payment is submitted, settled or failed in the ledger at most once each.

`ck_ledger_transaction_cites_something` is a constraint over rows that already
exist, and this migration refuses to run if any of them violate it rather than
deleting them or adding the constraint `NOT VALID`. A ledger transaction that
cites nothing is not a formatting problem to be tidied away in a migration; it
is a movement of money nobody can explain, and somebody has to look at it. The
error names the rows. (One turned up on the machine this was written on: a
fixture from `test_payments.py` left behind when the integration suite still ran
against the demo database, from before it owned one.)

Revision ID: 0012_two_roles
Revises: 0011_payment_integrity
Created: 2026-09-17 08:05:00.000000+00:00

"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0012_two_roles"
down_revision: str | None = "0011_payment_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API = "threshold_api"
WORKER = "threshold_worker"
FORMER = "threshold_app"

EVERY_TABLE = (
    "approval_decision",
    "audit_event",
    "dead_letter",
    "document",
    "escalation",
    "extraction",
    "goods_receipt",
    "goods_receipt_line",
    "idempotency_key",
    "job",
    "ledger_entry",
    "ledger_transaction",
    "match_exception",
    "match_result",
    "payment",
    "purchase_order",
    "purchase_order_line",
    "vendor",
    "vendor_bank_account",
)

API_INSERTS = ("document", "idempotency_key", "job", "audit_event", "approval_decision")

WORKER_INSERTS = (
    "extraction",
    "match_result",
    "match_exception",
    "escalation",
    "approval_decision",
    "payment",
    "ledger_transaction",
    "ledger_entry",
    "job",
    "dead_letter",
    "audit_event",
)

#: Unchanged from 0011; restated because the grantee changes.
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

KINDS = ("accrual", "payment_submitted", "payment_settled", "payment_failed", "opening")
ACCOUNTS = ("accounts_payable", "cash", "cash_in_transit", "expense", "opening_balance")

ACTOR_ROLES = """
CREATE OR REPLACE FUNCTION threshold_actor_roles() RETURNS text[]
LANGUAGE sql STABLE AS $roles$
    -- `true`: missing is not an error. `nullif`: a connection that has ever had
    -- this set reads '' afterwards, not NULL, and '' must not mean "no rules".
    SELECT string_to_array(nullif(current_setting('threshold.actor_roles', true), ''), ',')
$roles$;
"""

#: One statement per execute: asyncpg prepares, and a prepared statement may not
#: carry two commands.
MAY_DECIDE = """
CREATE OR REPLACE FUNCTION may_decide(result uuid, decision text) RETURNS boolean
LANGUAGE sql STABLE AS $decide$
    -- domain/approval.py authorise(), clause for clause.
    SELECT decision IN ('approved', 'rejected', 'held')      -- never 'auto_approved'
       AND threshold_actor_roles() IS NOT NULL               -- fail closed
       AND NOT (threshold_actor_roles() && ARRAY['auditor']) -- READ_ONLY_ROLES
       AND EXISTS (                                          -- something to decide
               SELECT 1 FROM match_exception e WHERE e.match_result_id = result)
       AND NOT EXISTS (                                      -- and all of it theirs
               SELECT 1 FROM match_exception e
                WHERE e.match_result_id = result
                  AND NOT (e.owner_role = ANY (threshold_actor_roles())))
$decide$;
"""

LEDGER_CITES = """
CREATE OR REPLACE FUNCTION ledger_entry_balanced() RETURNS trigger
LANGUAGE plpgsql AS $ledger$
DECLARE
    subject uuid;
    total bigint;
    moved bigint;
    header ledger_transaction%ROWTYPE;
    owed bigint;
    owed_in text;
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
        SELECT COALESCE(sum(amount_minor), 0),
               COALESCE(sum(amount_minor) FILTER (WHERE amount_minor > 0), 0)
          INTO total, moved
          FROM ledger_entry WHERE transaction_id = subject;
        IF total <> 0 THEN
            RAISE EXCEPTION
                'ledger transaction % does not balance: entries sum to %', subject, total
                USING ERRCODE = 'check_violation';
        END IF;

        -- Balanced is not true. What was moved must be what the cited row says.
        SELECT * INTO header FROM ledger_transaction WHERE id = subject;
        IF NOT FOUND OR moved = 0 THEN
            CONTINUE;  -- the transaction itself is being removed, or is now empty
        END IF;

        IF header.payment_id IS NOT NULL THEN
            SELECT amount_minor, currency INTO owed, owed_in
              FROM payment WHERE id = header.payment_id;
        ELSIF header.match_result_id IS NOT NULL THEN
            SELECT amount_minor, currency INTO owed, owed_in
              FROM match_result WHERE id = header.match_result_id;
        ELSE
            CONTINUE;  -- an opening balance cites nothing, and only the owner posts one
        END IF;

        IF moved IS DISTINCT FROM owed OR header.currency IS DISTINCT FROM owed_in THEN
            RAISE EXCEPTION
                'ledger transaction % moves % % but what it cites is for % %',
                subject, moved, header.currency, owed, owed_in
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NULL;
END
$ledger$;
"""

#: 0008's body, restored on downgrade.
LEDGER_BALANCES_ONLY = """
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


def _role(name: str, variable: str) -> None:
    """Create a LOGIN-only role, or re-key it if a password was configured.

    Same shape as 0010, including the lesson it learned the hard way: the role is
    cluster-wide and this runs per database, so a configured password is always
    applied and the local default never overwrites one.
    """
    configured = os.environ.get(variable)
    password = (configured or name).replace("'", "''")
    op.execute(
        f"""
        DO $role$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN
                EXECUTE format(
                    'CREATE ROLE {name} LOGIN PASSWORD %L '
                    'NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION',
                    '{password}'
                );
            ELSIF {"true" if configured else "false"} THEN
                EXECUTE format('ALTER ROLE {name} PASSWORD %L', '{password}');
            END IF;
        END
        $role$;
        """
    )


def upgrade() -> None:
    _role(API, "THRESHOLD_API_DB_PASSWORD")
    _role(WORKER, "THRESHOLD_WORKER_DB_PASSWORD")

    # The single role's grants go, in this database. The role itself stays: it is
    # cluster-wide, and another database here may still be at 0011 and using it.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {FORMER}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {FORMER}")

    for role in (API, WORKER):
        op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        for table in EVERY_TABLE:
            op.execute(f"GRANT SELECT ON {table} TO {role}")

    for table in API_INSERTS:
        op.execute(f"GRANT INSERT ON {table} TO {API}")
    for table in WORKER_INSERTS:
        op.execute(f"GRANT INSERT ON {table} TO {WORKER}")
    for table, columns in MUTABLE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(columns)}) ON {table} TO {WORKER}")
    op.execute(f"GRANT DELETE ON job TO {WORKER}")

    # ---- who may record which kind of decision -----------------------------
    op.execute(ACTOR_ROLES)
    op.execute(MAY_DECIDE)
    op.execute("ALTER TABLE approval_decision ENABLE ROW LEVEL SECURITY")
    # Reads are not policed here, deliberately. Hiding a decision or an exception
    # from a reader changes what `authorise()` computes -- an owner whose
    # exception is invisible stops being required -- so a read policy would be an
    # authorisation bug wearing a privacy control's clothes. See D-035.
    op.execute(
        f"CREATE POLICY approval_decision_read ON approval_decision "
        f"FOR SELECT TO {API}, {WORKER} USING (true)"
    )
    op.execute(
        f"CREATE POLICY approval_decision_by_a_person ON approval_decision "
        f"FOR INSERT TO {API} "
        f"WITH CHECK (actor_type = 'human' AND may_decide(match_result_id, decision))"
    )
    op.execute(
        f"CREATE POLICY approval_decision_by_the_system ON approval_decision "
        f"FOR INSERT TO {WORKER} "
        f"WITH CHECK (actor_type = 'system' AND decision = 'auto_approved' AND EXISTS ("
        f"SELECT 1 FROM match_result m WHERE m.id = match_result_id "
        f"AND m.disposition = 'auto_approve'))"
    )

    # ---- the ledger cites what it moves ------------------------------------
    op.execute(
        """
        DO $preflight$
        DECLARE
            orphans text;
        BEGIN
            SELECT string_agg(id::text || ' (' || kind || ', ' || created_at::date || ')', ', ')
              INTO orphans
              FROM ledger_transaction
             WHERE kind <> 'opening' AND num_nonnulls(payment_id, match_result_id) <> 1;
            IF orphans IS NOT NULL THEN
                RAISE EXCEPTION
                    'these ledger transactions cite nothing and are not opening balances: %. '
                    'Each is a movement of money with no subject. Reconcile them -- post a '
                    'reversing transaction, or remove them if they are known test residue -- '
                    'and run this migration again. It will not decide for you.', orphans
                    USING ERRCODE = 'check_violation';
            END IF;
        END
        $preflight$;
        """
    )

    kinds = ", ".join(f"'{k}'" for k in KINDS)
    accounts = ", ".join(f"'{a}'" for a in ACCOUNTS)
    op.execute(
        f"ALTER TABLE ledger_transaction ADD CONSTRAINT ck_ledger_transaction_kind "
        f"CHECK (kind IN ({kinds}))"
    )
    op.execute(
        f"ALTER TABLE ledger_entry ADD CONSTRAINT ck_ledger_entry_account "
        f"CHECK (account IN ({accounts}))"
    )
    op.execute(
        "ALTER TABLE ledger_transaction ADD CONSTRAINT ck_ledger_transaction_cites_something "
        "CHECK (kind = 'opening' OR num_nonnulls(payment_id, match_result_id) = 1)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ledger_transaction_once_per_payment "
        "ON ledger_transaction (payment_id, kind) WHERE payment_id IS NOT NULL"
    )
    op.execute(LEDGER_CITES)

    op.execute("ALTER TABLE ledger_transaction ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY ledger_transaction_read ON ledger_transaction "
        f"FOR SELECT TO {API}, {WORKER} USING (true)"
    )
    op.execute(
        f"CREATE POLICY ledger_transaction_by_the_worker ON ledger_transaction "
        f"FOR INSERT TO {WORKER} WITH CHECK (kind <> 'opening')"
    )


def downgrade() -> None:
    for policy in ("ledger_transaction_by_the_worker", "ledger_transaction_read"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON ledger_transaction")
    op.execute("ALTER TABLE ledger_transaction DISABLE ROW LEVEL SECURITY")
    op.execute(LEDGER_BALANCES_ONLY)
    op.execute("DROP INDEX IF EXISTS uq_ledger_transaction_once_per_payment")
    op.execute(
        "ALTER TABLE ledger_transaction DROP CONSTRAINT IF EXISTS "
        "ck_ledger_transaction_cites_something"
    )
    op.execute("ALTER TABLE ledger_entry DROP CONSTRAINT IF EXISTS ck_ledger_entry_account")
    op.execute(
        "ALTER TABLE ledger_transaction DROP CONSTRAINT IF EXISTS ck_ledger_transaction_kind"
    )

    for policy in (
        "approval_decision_by_the_system",
        "approval_decision_by_a_person",
        "approval_decision_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON approval_decision")
    op.execute("ALTER TABLE approval_decision DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS may_decide(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS threshold_actor_roles()")

    for role in (API, WORKER):
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
        op.execute(f"REVOKE USAGE ON SCHEMA public FROM {role}")

    # Back to the single role, exactly as 0011 left it.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {FORMER}")
    for table in EVERY_TABLE:
        op.execute(f"GRANT SELECT ON {table} TO {FORMER}")
    for table in sorted(set(API_INSERTS) | set(WORKER_INSERTS)):
        op.execute(f"GRANT INSERT ON {table} TO {FORMER}")
    for table, columns in MUTABLE_COLUMNS.items():
        op.execute(f"GRANT UPDATE ({', '.join(columns)}) ON {table} TO {FORMER}")
    op.execute(f"GRANT DELETE ON job TO {FORMER}")
