"""The application stops being a Postgres superuser.

Since M0 every process -- API, worker, migrations, seed, tests -- has connected
as `threshold`, which is what `POSTGRES_USER` creates: the bootstrap superuser,
and the owner of every table. Two consequences, neither of which anybody chose.

**Every database-enforced guarantee in this repository held against a buggy
application and not against a compromised one.** The append-only trigger on
`audit_event` and the balance trigger on `ledger_entry` are the claims this
project leans on hardest, and a superuser can `ALTER TABLE ... DISABLE TRIGGER`
either of them in one statement. "Postgres refuses" was true only while the
application was asking nicely.

**Row-level security would have been inert.** Superusers bypass it
unconditionally, and owners bypass it unless forced. Verified against a scratch
table with FORCE ROW LEVEL SECURITY and an unset policy GUC: every row visible.

So this revision creates `threshold_app` -- LOGIN and nothing else: not
superuser, not BYPASSRLS, not an owner of anything, unable to create roles or
databases -- and grants it what the application was observed to need, table by
table. `threshold` remains the owner and is what migrations and the seed run as.
Runtime and schema ownership become two identities.

## The grants are the append-only rules, a second time

`audit_event`, `ledger_transaction`, `ledger_entry`, `approval_decision`,
`extraction`, `match_result`, `match_exception`, `escalation`, `document` and
`idempotency_key` are SELECT + INSERT only. The application never updates or deletes any of them, by
design -- they are the record -- and until now nothing but discipline (and, for
two of them, a trigger it could have disabled) said so. A privilege the role
does not hold cannot be misused by a bug, an injected statement, or an ORM flush
somebody forgot about.

UPDATE is granted on exactly the tables whose rows have a lifecycle: `job`,
`dead_letter`, `payment`. DELETE on exactly one: `job`, for
the purge of finished work.

(Table-wide UPDATE turned out to be too wide, and the review of this migration
demonstrated it by redirecting a pending payment. 0011 narrows all three to the
columns that record how far along a row is.)

The reference data -- vendors, bank accounts, purchase orders, receipts -- is
SELECT only. The application reads a vendor's bank history to catch a redirect;
it has no business being able to rewrite it, because rewriting it is the attack.

## The role is cluster-wide and the grants are per-database

`CREATE ROLE` is guarded because this migration runs once per database
(`threshold`, `threshold_test`, `threshold_queue_tests`) against one cluster.

The password comes from `THRESHOLD_APP_DB_PASSWORD`, defaulting to a value fit
only for the local compose stack -- the same posture as `POSTGRES_PASSWORD:
threshold` two files away, and marked as such. A deployment sets the variable.

Revision ID: 0010_least_privilege
Revises: 0009_debts
Created: 2026-09-17 04:10:00.000000+00:00

"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0010_least_privilege"
down_revision: str | None = "0009_debts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "threshold_app"

#: The record. Written once, never changed, never removed.
APPEND_ONLY = (
    "audit_event",
    "ledger_transaction",
    "ledger_entry",
    "approval_decision",
    "extraction",
    "match_result",
    "match_exception",
    "escalation",
    "document",
    # Inserted once per key and only ever read back. It looked like it had a
    # lifecycle and does not: a replay returns the stored response unchanged.
    "idempotency_key",
)

#: Rows with a lifecycle: a job is claimed, a payment settles, a letter is replayed.
MUTABLE = ("job", "dead_letter", "payment")

#: Reference data the application consults and must not be able to rewrite.
READ_ONLY = (
    "vendor",
    "vendor_bank_account",
    "purchase_order",
    "purchase_order_line",
    "goods_receipt",
    "goods_receipt_line",
)


def upgrade() -> None:
    configured = os.environ.get("THRESHOLD_APP_DB_PASSWORD")
    password = configured or "threshold_app"
    # The role is cluster-wide and this runs once per database, so CREATE is
    # guarded. The first version stopped there, which meant whichever database
    # was migrated first fixed the password for the cluster: a laptop run with
    # no variable set created the role with the public default, and the later
    # run *with* the secret skipped CREATE without a word -- leaving an API that
    # could not authenticate and a migration that reported success.
    #
    # So an explicitly configured password is always applied. The default never
    # is: it must not overwrite a real secret because somebody ran the test
    # database's migrations from a shell without the variable.
    #
    # Quoted as a literal by the server-side format(), not interpolated here:
    # a password containing a quote must not become SQL.
    op.execute(
        f"""
        DO $role$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                EXECUTE format(
                    'CREATE ROLE {ROLE} LOGIN PASSWORD %L '
                    'NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION',
                    {_literal(password)}
                );
            ELSIF {"true" if configured else "false"} THEN
                EXECUTE format('ALTER ROLE {ROLE} PASSWORD %L', {_literal(password)});
            END IF;
        END
        $role$;
        """
    )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    # CONNECT is granted to PUBLIC by default; stated anyway so the intent
    # survives somebody tightening the default later.
    op.execute(
        f"DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database()); END $$"
    )

    for table in APPEND_ONLY:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {ROLE}")
    for table in MUTABLE:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {ROLE}")
    for table in READ_ONLY:
        op.execute(f"GRANT SELECT ON {table} TO {ROLE}")

    # The one DELETE: purging finished jobs.
    op.execute(f"GRANT DELETE ON job TO {ROLE}")

    # Matches nothing today: every key in this schema is a uuid. Kept because a
    # `serial` column added later needs USAGE on its sequence or INSERT fails
    # with "permission denied for sequence" -- but note that this statement will
    # not cover it, since it grants on the sequences that exist *now*. That
    # migration has to grant its own. (An identity column does not need it:
    # verified on Postgres 16, INSERT succeeds with no sequence privilege.)
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")

    # Deliberately NO `ALTER DEFAULT PRIVILEGES`. A table added next month gets
    # no grant until somebody decides which of the three lists it belongs on,
    # and `test_least_privilege.py` fails until they do. A default would make
    # that decision silently, in the permissive direction.


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    # The role itself is left in place: it is cluster-wide, and another database
    # in this cluster may still be at this revision and depending on it.


def _literal(value: str) -> str:
    """A SQL string literal for use *inside* the DO block's format() call."""
    return "'" + value.replace("'", "''") + "'"
