"""What the running application may do to the database, asserted as the application.

Until migration 0010 every process connected as the bootstrap superuser. The
append-only trigger on `audit_event` and the balance trigger on `ledger_entry`
were therefore advisory: the role they constrained could disable either in one
statement. This file connects as `threshold_app` -- the role the API and the
worker now hold -- and asks Postgres directly.

Two kinds of assertion, because either alone can be satisfied by accident.

**The policy, table by table.** `EXPECTED` is the privilege map as it stands
today, and it is compared against `has_table_privilege` for every table in the
schema. The migration is history and cannot be the source of truth for the
present: the table added next month is granted in *its* migration, and this map
is where somebody has to decide what that grant should be. A table missing from
the map fails the test, which is the point -- there is deliberately no
`ALTER DEFAULT PRIVILEGES`, so a new table is unusable until it is classified.

**The behaviour.** A privilege map that reads correctly proves nothing if the
role is quietly a member of something larger. So the statements an attacker
would actually send are sent, and each must be refused *for lack of privilege*
(SQLSTATE 42501) rather than by a trigger further in -- a trigger's refusal is
the second lock, and this file is about the first.

Requires: make up, make test-db
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from tests.integration.databases import ISOLATED, as_role

pytestmark = pytest.mark.integration

API = "threshold_api"
WORKER = "threshold_worker"
ROLES = (API, WORKER)

READ = frozenset({"SELECT"})
APPEND = frozenset({"SELECT", "INSERT"})

#: Every privilege Postgres can grant on a table. `EXPECTED` names the ones held;
#: the rest must be absent, including the two people forget are separate.
ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

#: What each runtime role holds, table by table, and nothing it does not.
#:
#: Both roles read everything: the console shows a payment beside the invoice it
#: paid, and the worker matches against the vendor's bank history. Reads are not
#: where the danger is, and narrowing them here would be the authorisation
#: inversion D-035 describes -- an exception a reader cannot see is an owner who
#: stops being required.
#:
#: Writes are where they differ, and the difference is the whole increment.
EXPECTED: dict[str, dict[str, frozenset[str]]] = {
    # The record. Written once, never changed, never removed -- and only the
    # process whose job it is may write each part.
    "extraction": {API: READ, WORKER: APPEND},
    "match_result": {API: READ, WORKER: APPEND},
    "match_exception": {API: READ, WORKER: APPEND},
    "escalation": {API: READ, WORKER: APPEND},
    "ledger_transaction": {API: READ, WORKER: APPEND},
    "ledger_entry": {API: READ, WORKER: APPEND},
    "payment": {API: READ, WORKER: APPEND},
    # Both write these, and they are the only three that both write. The API
    # records what a person did; the worker records what it did.
    "audit_event": {API: APPEND, WORKER: APPEND},
    "job": {API: APPEND, WORKER: APPEND | {"DELETE"}},  # the purge of finished work
    # Both may insert a decision, and a row-level policy decides which kind:
    # the API only a human's, the worker only `auto_approved` on a match result
    # that says auto_approve. `test_two_roles.py` is where that is asserted;
    # the grant alone cannot express it.
    "approval_decision": {API: APPEND, WORKER: APPEND},
    # Ingest is the API's: a document arrives over HTTP.
    "document": {API: APPEND, WORKER: READ},
    "idempotency_key": {API: APPEND, WORKER: READ},
    # The worker's own failure bookkeeping.
    "dead_letter": {API: READ, WORKER: APPEND},
    # Reference data. Rewriting it is the attack, for either of them.
    "vendor": {API: READ, WORKER: READ},
    "vendor_bank_account": {API: READ, WORKER: READ},
    "purchase_order": {API: READ, WORKER: READ},
    "purchase_order_line": {API: READ, WORKER: READ},
    "goods_receipt": {API: READ, WORKER: READ},
    "goods_receipt_line": {API: READ, WORKER: READ},
    # Nothing at runtime reads either: one is Alembic's, one is a worked example
    # of the money column convention.
    "alembic_version": {API: frozenset(), WORKER: frozenset()},
    "money_example": {API: frozenset(), WORKER: frozenset()},
}

#: How far along a row is. Everything not listed is what the row *is*, and no
#: runtime role can change it after the insert.
#:
#: Held by the worker alone since 0012: the API updates nothing, anywhere.
#:
#: This was a table-wide UPDATE for one migration. The review of that migration
#: used it to point a pending payment at another vendor's bank account.
MUTABLE_COLUMNS: dict[str, frozenset[str]] = {
    "payment": frozenset(
        {
            "state",
            "submitted_at",
            "settled_at",
            "rail_attempts",
            "rail_reference",
            "failure_reason",
        }
    ),
    "job": frozenset(
        {
            "state",
            "attempts",
            "run_after",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "last_error",
            "updated_at",
        }
    ),
    "dead_letter": frozenset({"replayed_at"}),
}

INSUFFICIENT_PRIVILEGE = "42501"


@pytest_asyncio.fixture(params=ROLES)
async def app(request: pytest.FixtureRequest) -> AsyncIterator[asyncpg.Connection]:
    """A connection as one of the runtime roles -- every test here runs twice.

    Parameterised rather than written twice because almost everything in this
    file is true of both: neither may escalate, neither may rewrite the record,
    neither owns anything. Where they differ, the test reads `EXPECTED[table]
    [role]` or names the role outright.
    """
    role: str = request.param
    try:
        connection = await asyncpg.connect(as_role(role), timeout=5)
    except (OSError, asyncpg.PostgresError) as unreachable:
        pytest.skip(f"cannot connect as {role}; `make up` and `make test-db` ({unreachable})")
    try:
        yield connection
    finally:
        await connection.close()


async def _role(app: asyncpg.Connection) -> str:
    """Asked of the connection rather than taken from the parameter: a DSN that
    fell back to another identity must not be able to satisfy this file."""
    return str(await app.fetchval("select current_user"))


@pytest_asyncio.fixture
async def owner() -> AsyncIterator[asyncpg.Connection]:
    connection = await asyncpg.connect(ISOLATED.replace("+asyncpg", ""), timeout=5)
    try:
        yield connection
    finally:
        await connection.close()


async def _refused(app: asyncpg.Connection, statement: str, *args: object) -> str:
    """Run a statement that must be refused; return the SQLSTATE it was refused with.

    Inside a transaction that is rolled back either way, so a statement that is
    wrongly *permitted* leaves nothing behind before the assertion fails.
    """
    transaction = app.transaction()
    await transaction.start()
    try:
        await app.execute(statement, *args)
    except asyncpg.PostgresError as refusal:
        return str(refusal.sqlstate)
    finally:
        await transaction.rollback()
    return "permitted"


class TestTheRoleItself:
    async def test_it_is_who_the_test_thinks_it_is(self, app: asyncpg.Connection) -> None:
        """The vacuity guard for the whole file: a DSN that silently fell back to
        the owner would make every behavioural test below fail loudly, but would
        make this one the first and clearest."""
        assert await _role(app) in ROLES
        assert await app.fetchval("select session_user") == await _role(app)

    async def test_it_holds_no_attribute_that_outranks_a_grant(
        self, app: asyncpg.Connection
    ) -> None:
        row = await app.fetchrow(
            "select rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
            "from pg_roles where rolname = current_user"
        )
        assert row is not None
        held = sorted(name for name, value in row.items() if value)
        assert not held, (
            f"{await _role(app)} holds {held}. rolsuper skips every privilege check and "
            "every row-level-security policy; rolbypassrls skips the policies alone."
        )

    async def test_it_owns_nothing(self, app: asyncpg.Connection) -> None:
        """An owner may ALTER, DROP, DISABLE TRIGGER and re-grant to itself,
        whatever the ACL says. Owning nothing is what makes the ACL the truth."""
        owned = await app.fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and pg_get_userbyid(c.relowner) = current_user"
        )
        assert not owned, f"{await _role(app)} owns {[r['relname'] for r in owned]}"

    async def test_it_is_a_member_of_nothing(self, app: asyncpg.Connection) -> None:
        """Membership is how a harmless-looking role inherits a dangerous one:
        `pg_write_server_files`, `pg_execute_server_program`, or the owner."""
        memberships = await app.fetch(
            "select r.rolname from pg_auth_members m join pg_roles r on r.oid = m.roleid "
            "where m.member = (select oid from pg_roles where rolname = current_user)"
        )
        assert not memberships, (
            f"{await _role(app)} is a member of {[r['rolname'] for r in memberships]}"
        )


class TestThePolicyTableByTable:
    async def test_every_table_is_classified(self, app: asyncpg.Connection) -> None:
        tables = {
            r["tablename"]
            for r in await app.fetch("select tablename from pg_tables where schemaname = 'public'")
        }
        unclassified = sorted(tables - EXPECTED.keys())
        assert not unclassified, (
            f"{unclassified} exist and nobody has decided what each role may do to them. "
            "Grant in the migration that creates the table, then add a line to EXPECTED "
            "saying what the API and the worker may each do: APPEND if that process "
            "writes it, READ if it only consults it."
        )
        stale = sorted(EXPECTED.keys() - tables)
        assert not stale, f"EXPECTED describes tables that do not exist: {stale}"

    async def test_the_grants_are_exactly_the_policy(self, app: asyncpg.Connection) -> None:
        """Exactly: a missing grant is an outage, an extra one is the finding."""
        role = await _role(app)
        wrong: list[str] = []
        for table, by_role in sorted(EXPECTED.items()):
            expected = by_role[role]
            held = {
                privilege
                for privilege in ALL_PRIVILEGES
                if await app.fetchval(
                    "select has_table_privilege(current_user, $1, $2)",
                    f"public.{table}",
                    privilege,
                )
            }
            if held != expected:
                wrong.append(
                    f"{table}: extra {sorted(held - expected)}, missing {sorted(expected - held)}"
                )
        assert not wrong, f"{role}:\n" + "\n".join(wrong)

    async def test_the_column_grants_are_exactly_the_policy(self, app: asyncpg.Connection) -> None:
        """`has_table_privilege` is false for a table where only *some* columns
        are granted, so the test above cannot see these at all -- in either
        direction. A column-level UPDATE on `audit_event` would pass it.

        `column_privileges` also lists every column of a table-level grant, so
        the comparison is: anything there that the table policy does not explain
        must be an UPDATE on a column this map names, and every column this map
        names must be there.
        """
        rows = await app.fetch(
            "select table_name, column_name, privilege_type "
            "from information_schema.column_privileges "
            "where grantee = current_user and table_schema = 'public'"
        )
        role = await _role(app)
        beyond: set[tuple[str, str, str]] = {
            (r["table_name"], r["column_name"], r["privilege_type"])
            for r in rows
            if r["privilege_type"] not in EXPECTED.get(r["table_name"], {}).get(role, frozenset())
        }
        # The API updates nothing at all, so its half of this is the empty set.
        policy = (
            {
                (table, column, "UPDATE")
                for table, columns in MUTABLE_COLUMNS.items()
                for column in columns
            }
            if role == WORKER
            else set()
        )
        assert beyond == policy, (
            f"granted and not in the policy: {sorted(beyond - policy)}; "
            f"in the policy and not granted: {sorted(policy - beyond)}"
        )


class TestWhatACompromisedApplicationIsRefused:
    """Each statement is one an attacker with the application's connection would
    send. All are refused with 42501, before any trigger is consulted."""

    @pytest.mark.parametrize(
        "statement",
        [
            "update audit_event set action = 'nothing.happened'",
            "delete from audit_event",
            "update ledger_entry set amount_minor = 0",
            "delete from ledger_entry",
            "delete from ledger_transaction",
            "update approval_decision set decision = 'approved'",
            "delete from approval_decision",
            "update match_result set disposition = 'auto_approve'",
            "delete from match_exception",
            "delete from escalation",
            "delete from payment",
            "delete from dead_letter",
        ],
    )
    async def test_the_record_cannot_be_rewritten(
        self, app: asyncpg.Connection, statement: str
    ) -> None:
        assert await _refused(app, statement) == INSUFFICIENT_PRIVILEGE

    @pytest.mark.parametrize(
        "statement",
        [
            # The two statements the review of 0010 ran, and was not refused.
            "update payment set amount_minor = amount_minor * 5",
            "update payment set vendor_bank_account_id = gen_random_uuid()",
            "update payment set vendor_id = gen_random_uuid()",
            "update payment set approval_decision_id = gen_random_uuid()",
            "update payment set idempotency_key = 'thr-pay-again'",
            "update payment set currency = 'USD'",
            # A job's kind and payload are what it will do; its state is how far along it is.
            "update job set payload = '{}'::jsonb",
            "update job set kind = 'execute_payment'",
            "update job set dedupe_key = null",
            "update job set max_attempts = 1000000",
            "update dead_letter set payload = '{}'::jsonb",
            "update dead_letter set final_error = 'nothing happened'",
        ],
    )
    async def test_what_a_row_is_cannot_be_changed_only_how_far_along_it_is(
        self, app: asyncpg.Connection, statement: str
    ) -> None:
        assert await _refused(app, statement) == INSUFFICIENT_PRIVILEGE

    @pytest.mark.parametrize(
        "statement",
        [
            "update vendor_bank_account set account_fingerprint = 'attacker'",
            "insert into vendor_bank_account select * from vendor_bank_account",
            "update vendor set legal_name = 'redirected'",
            "update purchase_order_line set unit_price = 0",
            "delete from goods_receipt_line",
        ],
    )
    async def test_the_reference_data_cannot_be_rewritten(
        self, app: asyncpg.Connection, statement: str
    ) -> None:
        """The bank-detail comparison is the control against payment redirection,
        and it compares against these rows."""
        assert await _refused(app, statement) == INSUFFICIENT_PRIVILEGE

    @pytest.mark.parametrize(
        "statement",
        [
            "alter table audit_event disable trigger all",
            "alter table ledger_entry disable trigger all",
            "drop trigger trg_audit_event_append_only on audit_event",
            "drop trigger ck_ledger_entry_balanced on ledger_entry",
            "alter table approval_decision drop constraint uq_approval_decision_terminal",
            "drop table audit_event",
            # TRUNCATE is its own privilege, fires no row trigger, and is not DELETE.
            "truncate audit_event",
            "truncate job",
            # Disables every ordinary trigger for the session without touching a table.
            "set session_replication_role = replica",
        ],
    )
    async def test_the_second_lock_cannot_be_removed(
        self, app: asyncpg.Connection, statement: str
    ) -> None:
        assert await _refused(app, statement) == INSUFFICIENT_PRIVILEGE

    @pytest.mark.parametrize(
        "statement",
        [
            "set role threshold",
            "alter role threshold_app superuser",
            "alter role threshold_app bypassrls",
            "create role accomplice superuser",
            "create table public.staging (x int)",
            "create function public.f() returns int language sql as 'select 1'",
            "copy audit_event to program 'cat'",
            "select pg_read_file('/etc/passwd')",
            "select rolpassword from pg_authid",
            "select passwd from pg_shadow",
        ],
    )
    async def test_it_cannot_become_more_than_it_is(
        self, app: asyncpg.Connection, statement: str
    ) -> None:
        assert await _refused(app, statement) == INSUFFICIENT_PRIVILEGE

    async def test_it_cannot_grant_itself_what_it_lacks(self, app: asyncpg.Connection) -> None:
        """Asserted by effect, because Postgres does not refuse this one.

        A role holding *some* privilege on a table, and no grant option, gets a
        WARNING ("no privileges were granted") and a successful statement. The
        first version of this test expected 42501 and failed against a database
        that was behaving correctly.
        """
        transaction = app.transaction()
        await transaction.start()
        try:
            await app.execute("grant update, delete on audit_event to threshold_app")
            widened = [
                privilege
                for privilege in ("UPDATE", "DELETE")
                if await app.fetchval(
                    "select has_table_privilege(current_user, 'public.audit_event', $1)", privilege
                )
            ]
        finally:
            await transaction.rollback()
        assert not widened, f"{await _role(app)} granted itself {widened} on audit_event"

    async def test_it_holds_no_grant_option(self, app: asyncpg.Connection) -> None:
        """The reason the statement above is harmless, stated directly. WITH GRANT
        OPTION would let a compromised application hand its access to a role of
        its choosing."""
        grantable = await app.fetch(
            "select table_name, privilege_type from information_schema.role_table_grants "
            "where grantee = current_user and table_schema = 'public' and is_grantable = 'YES'"
        )
        assert not grantable, [f"{r['table_name']}:{r['privilege_type']}" for r in grantable]


class TestWhatItIsStillAbleToDo:
    """The vacuity guard for the refusals. A role that can do nothing at all
    passes every test above, and so does a connection that is simply broken."""

    async def test_a_job_lives_its_whole_life(self, app: asyncpg.Connection) -> None:
        """Insert, claim with SKIP LOCKED, finish, purge: every verb the queue uses.

        The worker's, again: the API enqueues work and never runs it.
        """
        if await _role(app) != WORKER:
            pytest.skip("the API enqueues jobs and never claims, finishes or purges one")
        transaction = app.transaction()
        await transaction.start()
        try:
            job_id = await app.fetchval(
                "insert into job (id, kind, payload) values ($1, 'privilege_probe', $2::jsonb) "
                "returning id",
                uuid.uuid4(),
                json.dumps({}),
            )
            claimed = await app.fetchval(
                "select id from job where id = $1 for update skip locked", job_id
            )
            assert claimed == job_id
            assert await app.execute("update job set attempts = attempts + 1 where id = $1", job_id)
            assert await app.execute("delete from job where id = $1", job_id) == "DELETE 1"
        finally:
            await transaction.rollback()

    async def test_the_record_can_be_read_and_appended_to(
        self, app: asyncpg.Connection, owner: asyncpg.Connection
    ) -> None:
        """The refusal of `UPDATE audit_event` means something only if
        `INSERT audit_event` is allowed from the same connection."""
        assert await app.fetchval("select count(*) from audit_event") >= 0
        columns = {
            r["column_name"]: r
            for r in await owner.fetch(
                "select column_name, is_nullable, column_default from information_schema.columns "
                "where table_schema = 'public' and table_name = 'audit_event'"
            )
        }
        assert "action" in columns, sorted(columns)

        assert (
            await _refused(app, "insert into audit_event default values") != INSUFFICIENT_PRIVILEGE
        ), "refused for lack of privilege; a NOT NULL violation is the expected refusal here"

    @pytest.mark.parametrize(
        "statement",
        [
            # `where false`: the privilege is checked before any row is looked at,
            # so this asks "may you?" without changing anything or meeting a trigger.
            "update payment set state = state, rail_attempts = rail_attempts where false",
            "update job set state = state, attempts = attempts, updated_at = now() where false",
            "update dead_letter set replayed_at = now() where false",
            "select id from job for update skip locked limit 0",
        ],
    )
    async def test_how_far_along_a_row_is_can_still_be_written(
        self, app: asyncpg.Connection, statement: str
    ) -> None:
        """The other half of the column policy. Without it, revoking UPDATE
        entirely would pass every refusal above.

        The worker's alone: the API holds no UPDATE on anything, which
        `test_two_roles.py` asserts directly.
        """
        expected = "permitted" if await _role(app) == WORKER else INSUFFICIENT_PRIVILEGE
        assert await _refused(app, statement) == expected

    async def test_reference_data_can_be_read(self, app: asyncpg.Connection) -> None:
        assert await app.fetchval("select count(*) from vendor") > 0, "`make test-db` seeds vendors"
        assert await app.fetchval("select count(*) from vendor_bank_account") > 0
