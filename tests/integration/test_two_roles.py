"""What each process may write, asked of Postgres as that process.

Migration 0012. D-034 ended by admitting what a compromised application could
still do: hold `INSERT` on `match_result` and `approval_decision`, forge a
consistent chain from the root, and pay it. That admission treated "the
application" as one thing. It is two, and they trust each other for no reason:
the worker parses documents sent by strangers, and the API is the only process
that knows who a person is.

So the tests here are the forgeries, attempted as each role. The worker tries to
record that a human approved an invoice the match blocked; the API tries to
write the match result, the payment and the ledger entry that would let it forge
from the root. Both are refused, and the refusal is a `SELECT`-able fact about
the database rather than a branch in the application.

The second group is about `may_decide()`, which is `domain/approval.py::
authorise()` written again in SQL. It is worth being exact about what that
proves. A compromised API can call `set_config` with any roles it likes, so this
is not a defence against a compromised API. It is a defence against this
codebase: a route that inserts a decision without having asked `authorise()` is
refused. The tests are shaped accordingly -- they set the GUC by hand, exactly as
an honest caller would, and check that Postgres reaches the same verdict the
domain function does for the same inputs.

Requires: make up, make test-db
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.databases import ISOLATED, as_role
from tests.integration.test_matching import _document_with_extraction, _invoice_fields

from threshold.domain.approval import Authorisation, Decision, OpenException, authorise
from threshold.domain.models import MatchResult
from threshold.domain.roles import Role
from threshold.domain.taxonomy import ExceptionCode, spec_for
from threshold.services.matching import MatchingService

pytestmark = pytest.mark.integration

#: Postgres reports the same class for a missing grant and for a row a policy
#: refused. The *message* differs, and the difference is the diagnosis -- "you
#: may not write this table" against "you may not write this row" -- so both are
#: asserted wherever the distinction is the point.
INSUFFICIENT_PRIVILEGE = "42501"
NO_GRANT = "permission denied for table"
NO_POLICY = "violates row-level security policy"

#: What a CHECK constraint or a trigger raises. A different diagnosis again:
#: the role may write this table, and the row itself is not allowed to exist.
CHECK_VIOLATION = "23514"


@pytest_asyncio.fixture
async def api() -> AsyncIterator[asyncpg.Connection]:
    async for connection in _connect("threshold_api"):
        yield connection


@pytest_asyncio.fixture
async def worker() -> AsyncIterator[asyncpg.Connection]:
    async for connection in _connect("threshold_worker"):
        yield connection


async def _connect(role: str) -> AsyncIterator[asyncpg.Connection]:
    try:
        connection = await asyncpg.connect(as_role(role), timeout=5)
    except (OSError, asyncpg.PostgresError) as unreachable:
        pytest.skip(f"cannot connect as {role}; `make up` and `make test-db` ({unreachable})")
    try:
        assert await connection.fetchval("select current_user") == role
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def owner() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(ISOLATED)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres is not reachable; `make up`")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _refused(
    connection: asyncpg.Connection, statement: str, *args: object
) -> tuple[str, str]:
    """Attempt it inside a transaction that is rolled back either way.

    Returns the SQLSTATE and the message. Both, because a statement naming a
    column that does not exist is refused with 42703 before any privilege is
    considered -- two of the statements below did exactly that when they were
    first written, and passed a test that asserted only on the code.
    """
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(statement, *args)
    except asyncpg.PostgresError as refusal:
        return str(refusal.sqlstate), str(refusal)
    finally:
        await transaction.rollback()
    return "permitted", ""


async def _blocked_invoice(sessions: async_sessionmaker[AsyncSession]) -> MatchResult:
    """One real invoice, blocked on a price variance, which the buyer owns."""
    async with sessions() as session:
        document = await _document_with_extraction(
            session,
            _invoice_fields(
                invoice_number=f"ROLE-{uuid.uuid4().hex[:8].upper()}",
                unit_price="21.0000",  # PO-44718 agrees 18.40
            ),
        )
        await session.flush()
        result = await MatchingService().match_document(session, document.id)
        assert result.disposition == "blocked", result.disposition
        await session.commit()
        return (
            await session.execute(select(MatchResult).where(MatchResult.id == result.id))
        ).scalar_one()


DECISION = (
    "insert into approval_decision (match_result_id, document_id, decision, actor_type, "
    "actor_id, actor_label, reason, overrode) "
    "values ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb)"
)


class TestTheWorkerCannotSpeakForAPerson:
    """The worker runs a PDF parser over bytes a stranger sent. It is the
    process most likely to be taken over, and the one with the least business
    recording that somebody approved anything."""

    async def test_it_cannot_record_a_human_decision(
        self, worker: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        result = await _blocked_invoice(owner)
        refusal = await _refused(
            worker,
            DECISION,
            result.id,
            result.document_id,
            "approved",
            "human",
            "attacker-sub",
            "A Person Who Does Not Exist",
            "looks fine to me",
        )
        code, message = refusal
        assert (code, NO_POLICY in message) == (INSUFFICIENT_PRIVILEGE, True), f"{code}: {message}"

    async def test_it_cannot_auto_approve_what_the_match_blocked(
        self, worker: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        """The forgery from D-034, reduced to its load-bearing step. An invoice
        the match blocked is released by a person; this row says the system
        released it, and the policy reads the match result to check."""
        result = await _blocked_invoice(owner)
        refusal = await _refused(
            worker,
            DECISION,
            result.id,
            result.document_id,
            "auto_approved",
            "system",
            None,
            None,
            None,
        )
        code, message = refusal
        assert (code, NO_POLICY in message) == (INSUFFICIENT_PRIVILEGE, True), f"{code}: {message}"

    async def test_it_can_auto_approve_what_the_match_cleared(
        self, worker: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        """The vacuity guard, and the worker's actual job. A policy that refused
        everything would pass both tests above."""
        result_id = await _match_result_with(owner, ())
        document_id = await worker.fetchval(
            "select document_id from match_result where id = $1", result_id
        )
        code, message = await _refused(
            worker, DECISION, result_id, document_id, "auto_approved", "system", None, None, None
        )
        assert (code, message) == ("permitted", ""), f"{code}: {message}"


class TestTheApiCannotForgeFromTheRoot:
    """It authenticates people and records what they decide. It never extracts,
    matches, pays or posts -- so a compromised API cannot manufacture the
    evidence its own decision would be about."""

    @pytest.mark.parametrize(
        "statement",
        [
            "insert into match_result (document_id, extraction_id, version, disposition, "
            "policy_version) values (gen_random_uuid(), gen_random_uuid(), 1, 'auto_approve', 'x')",
            "insert into match_exception (match_result_id, code, severity, owner_role, due_at) "
            "values (gen_random_uuid(), 'price_variance', 'block', 'buyer', now())",
            "insert into extraction (document_id, version, extractor_kind, extractor_id, "
            "fields) values (gen_random_uuid(), 1, 'deterministic', 'x', '{}'::jsonb)",
            "insert into payment (approval_decision_id, match_result_id, document_id, vendor_id, "
            "vendor_bank_account_id, amount_minor, currency, currency_exponent, state, "
            "idempotency_key, rail_id) values (gen_random_uuid(), gen_random_uuid(), "
            "gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 1, 'EUR', 2, 'pending', "
            "'thr-pay-forged', 'x')",
            "insert into ledger_transaction (kind, currency, currency_exponent) "
            "values ('opening', 'EUR', 2)",
            "insert into ledger_entry (transaction_id, account, amount_minor) "
            "values (gen_random_uuid(), 'cash', 1)",
            "insert into escalation (match_result_id, owner_role, codes, summary, triager_id) "
            "values (gen_random_uuid(), 'buyer', '[\"price_variance\"]'::jsonb, 'x', 'x')",
        ],
    )
    async def test_it_cannot_write_the_evidence(
        self, api: asyncpg.Connection, statement: str
    ) -> None:
        code, message = await _refused(api, statement)
        assert (code, NO_GRANT in message) == (INSUFFICIENT_PRIVILEGE, True), f"{code}: {message}"

    async def test_it_cannot_auto_approve(
        self, api: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        """`auto_approved` means "no person looked", and this is the process
        whose entire purpose is that a person looked."""
        result = await _blocked_invoice(owner)
        refusal = await _refused(
            api,
            DECISION,
            result.id,
            result.document_id,
            "auto_approved",
            "system",
            None,
            None,
            None,
        )
        code, message = refusal
        assert (code, NO_POLICY in message) == (INSUFFICIENT_PRIVILEGE, True), f"{code}: {message}"

    async def test_it_updates_and_deletes_nothing_at_all(self, api: asyncpg.Connection) -> None:
        held = await api.fetch(
            "select table_name, privilege_type from information_schema.role_table_grants "
            "where grantee = current_user and privilege_type in ('UPDATE', 'DELETE')"
        )
        assert not held, [f"{r['table_name']}:{r['privilege_type']}" for r in held]

    async def test_it_can_still_do_its_own_job(self, api: asyncpg.Connection) -> None:
        """The vacuity guard: a role granted nothing passes every test above."""
        transaction = api.transaction()
        await transaction.start()
        try:
            await api.execute(
                "insert into document (source, original_filename, content_type, byte_size, "
                "content_sha256, storage_key) values ('fixture', 'f.pdf', 'application/pdf', "
                "1, $1, $2)",
                uuid.uuid4().hex * 2,
                f"fixture/{uuid.uuid4()}",
            )
            await api.execute(
                "insert into job (kind, payload) values ('extract_document', '{}'::jsonb)"
            )
            await api.execute(
                "insert into audit_event (actor_type, action, entity_type, entity_id) "
                "values ('system', 'test.probe', 'document', gen_random_uuid())"
            )
        finally:
            await transaction.rollback()


#: Every combination `authorise()` distinguishes, as (roles held, exceptions).
CASES: tuple[tuple[str, frozenset[Role], tuple[ExceptionCode, ...]], ...] = (
    ("the owner of the only exception", frozenset({Role.BUYER}), (ExceptionCode.PRICE_VARIANCE,)),
    ("somebody else entirely", frozenset({Role.RECEIVING}), (ExceptionCode.PRICE_VARIANCE,)),
    # Two *different* owners. The first version of these two cases used
    # `quantity_variance`, which the buyer owns just as `price_variance` is the
    # buyer's -- so "one owner of two" held every owner there was, and a
    # `may_decide` weakened to "owns at least one of them" passed it. The
    # taxonomy is the thing to check before writing a case like this.
    (
        "one owner of two",
        frozenset({Role.BUYER}),
        (ExceptionCode.PRICE_VARIANCE, ExceptionCode.RECEIPT_MISSING),
    ),
    (
        "both owners of two",
        frozenset({Role.BUYER, Role.RECEIVING}),
        (ExceptionCode.PRICE_VARIANCE, ExceptionCode.RECEIPT_MISSING),
    ),
    (
        "an auditor who also owns it",
        frozenset({Role.AUDITOR, Role.BUYER}),
        (ExceptionCode.PRICE_VARIANCE,),
    ),
    (
        "a controller, who owns nothing",
        frozenset({Role.CONTROLLER}),
        (ExceptionCode.PRICE_VARIANCE,),
    ),
    ("nobody at all", frozenset(), (ExceptionCode.PRICE_VARIANCE,)),
    ("an invoice that raised nothing", frozenset({Role.BUYER}), ()),
)


class TestPostgresReachesTheSameVerdictAsTheDomain:
    """`may_decide()` and `authorise()` are one rule in two languages, and only a
    test keeps them the same."""

    @pytest.mark.parametrize(("label", "held", "codes"), CASES, ids=[c[0] for c in CASES])
    @pytest.mark.parametrize("decision", list(Decision), ids=lambda d: d.value)
    async def test_for_every_case_the_domain_distinguishes(
        self,
        api: asyncpg.Connection,
        owner: async_sessionmaker[AsyncSession],
        label: str,
        held: frozenset[Role],
        codes: tuple[ExceptionCode, ...],
        decision: Decision,
    ) -> None:
        result_id = await _match_result_with(owner, codes)
        await api.execute(
            "select set_config('threshold.actor_roles', $1, false)",
            ",".join(sorted(role.value for role in held)),
        )
        permitted = await api.fetchval("select may_decide($1, $2)", result_id, decision.value)

        expected: Authorisation = authorise(
            holder=held,
            exceptions=tuple(
                OpenException(
                    code=code, severity=spec_for(code).severity, owner=spec_for(code).owner
                )
                for code in codes
            ),
            decision=decision,
        )
        assert permitted is expected.permitted, (
            f"{label}, {decision.value}: Postgres says {permitted}, "
            f"the domain says {expected.permitted} because {expected.reason}"
        )

    async def test_an_unset_role_list_permits_nothing(
        self, api: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        """Fail closed. A connection that has never had the setting reads NULL;
        one recycled from a transaction that did reads `''`, which is why the
        function uses `nullif` rather than trusting NULL."""
        result_id = await _match_result_with(owner, (ExceptionCode.PRICE_VARIANCE,))
        for value, description in ((None, "never set"), ("", "recycled connection")):
            if value is None:
                await api.execute("reset threshold.actor_roles")
            else:
                await api.execute("select set_config('threshold.actor_roles', $1, false)", value)
            assert not await api.fetchval("select may_decide($1, 'approved')", result_id), (
                description
            )

    async def test_the_setting_does_not_survive_the_transaction(
        self, api: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        """`set_config(..., true)` is what the service uses, and the reason is a
        pooled connection: a session-level SET would hand the next request
        whoever the last one was."""
        async with api.transaction():
            await api.execute("select set_config('threshold.actor_roles', 'buyer', true)")
            assert await api.fetchval("select current_setting('threshold.actor_roles', true)") == (
                "buyer"
            )
        leaked = await api.fetchval("select current_setting('threshold.actor_roles', true)")
        assert leaked in (None, ""), f"the next request would inherit {leaked!r}"

    @pytest.mark.parametrize(
        ("roles", "permitted"),
        [
            ("buyer", True),
            ("receiving", False),
            ("auditor,buyer", False),
            ("", False),
        ],
    )
    async def test_the_policy_and_not_only_the_function(
        self,
        api: asyncpg.Connection,
        owner: async_sessionmaker[AsyncSession],
        roles: str,
        permitted: bool,
    ) -> None:
        """Through an INSERT, which is the path production takes.

        Everything above calls `may_decide()` directly. A mutation that removed
        it from the policy altogether -- leaving `actor_type = 'human'` as the
        only condition -- survived all of it, because nothing here had ever
        written a row *through* the policy.
        """
        result_id = await _match_result_with(owner, (ExceptionCode.PRICE_VARIANCE,))
        document_id = await api.fetchval(
            "select document_id from match_result where id = $1", result_id
        )
        transaction = api.transaction()
        await transaction.start()
        try:
            await api.execute("select set_config('threshold.actor_roles', $1, true)", roles)
            code, message = await _refused(
                api,
                DECISION,
                result_id,
                document_id,
                "approved",
                "human",
                "buyer-sub",
                "A Buyer",
                "agreed by phone",
            )
        finally:
            await transaction.rollback()

        if permitted:
            assert (code, message) == ("permitted", ""), f"{code}: {message}"
        else:
            assert (code, NO_POLICY in message) == (INSUFFICIENT_PRIVILEGE, True), (
                f"{code}: {message}"
            )

    async def test_the_api_cannot_write_a_decision_no_person_took(
        self, api: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        """`actor_type` is a separate clause from `may_decide`, and a mutation
        that dropped it survived until this existed: with the roles set
        correctly, the API could have recorded an `approved` decision attributed
        to the system -- a decision with no person behind it, in the table whose
        whole purpose is saying which person."""
        result_id = await _match_result_with(owner, (ExceptionCode.PRICE_VARIANCE,))
        document_id = await api.fetchval(
            "select document_id from match_result where id = $1", result_id
        )
        transaction = api.transaction()
        await transaction.start()
        try:
            await api.execute("select set_config('threshold.actor_roles', 'buyer', true)")
            code, message = await _refused(
                api, DECISION, result_id, document_id, "approved", "system", None, None, None
            )
        finally:
            await transaction.rollback()
        assert (code, NO_POLICY in message) == (INSUFFICIENT_PRIVILEGE, True), f"{code}: {message}"

    def test_the_cases_cover_what_the_domain_branches_on(self) -> None:
        """The vacuity guard. Every distinct reason `authorise()` can give must
        appear at least once above, or the comparison is against a rule nobody
        exercised."""
        reasons = {
            authorise(
                holder=held,
                exceptions=tuple(
                    OpenException(
                        code=code, severity=spec_for(code).severity, owner=spec_for(code).owner
                    )
                    for code in codes
                ),
                decision=Decision.APPROVED,
            ).reason.split(";")[0]
            for _label, held, codes in CASES
        }
        assert len(reasons) >= 4, reasons


async def _match_result_with(
    sessions: async_sessionmaker[AsyncSession], codes: tuple[ExceptionCode, ...]
) -> uuid.UUID:
    """A match result carrying exactly these exception codes.

    The document and the extraction come from the matching suite's own fixture
    rather than being built here: the first version of this function wrote both
    by hand and got three column names wrong, which is the kind of error a test
    should not be able to make about a schema it is asserting on.
    """
    from datetime import UTC, datetime, timedelta

    from threshold.domain.models import Extraction, MatchException

    async with sessions() as session:
        document = await _document_with_extraction(
            session, _invoice_fields(invoice_number=f"ROLE-{uuid.uuid4().hex[:8].upper()}")
        )
        await session.flush()
        extraction_id = (
            await session.execute(
                select(Extraction.id).where(Extraction.document_id == document.id)
            )
        ).scalar_one()

        result = MatchResult(
            document_id=document.id,
            extraction_id=extraction_id,
            version=1,
            disposition="blocked" if codes else "auto_approve",
            policy_version="2026.09.2",
        )
        session.add(result)
        await session.flush()
        for code in codes:
            spec = spec_for(code)
            session.add(
                MatchException(
                    match_result_id=result.id,
                    code=code.value,
                    severity=spec.severity.value,
                    owner_role=spec.owner.value,
                    due_at=datetime.now(UTC) + timedelta(hours=spec.sla_hours),
                    detail="for a test of who may decide",
                )
            )
        await session.commit()
        return result.id


class TestTheLedgerCitesWhatItMoves:
    """Two confirmed review findings: a balanced transaction referring to
    nothing, and one with an invented kind, account and currency. Both
    committed. Balanced is not true.

    Every transaction here cites a real match result, built by the owner --
    except the one testing that citing nothing is refused. Otherwise
    `ck_ledger_transaction_cites_something` fires first and the test passes
    having proved nothing about the constraint it names, which is what the
    first version of the `invented-kind` case did.
    """

    async def test_a_transaction_that_cites_nothing_is_refused(
        self, worker: asyncpg.Connection
    ) -> None:
        code, message = await _refused(
            worker,
            "insert into ledger_transaction (kind, currency, currency_exponent) "
            "values ('accrual', 'EUR', 2)",
        )
        assert code == CHECK_VIOLATION, f"{code}: {message}"
        assert "ck_ledger_transaction_cites_something" in message, message

    async def test_an_invented_kind_is_refused(
        self, worker: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        subject = await _match_result_with(owner, (ExceptionCode.PRICE_VARIANCE,))
        code, message = await _refused(
            worker,
            "insert into ledger_transaction (kind, currency, currency_exponent, match_result_id) "
            "values ('totally_made_up', 'EUR', 2, $1)",
            subject,
        )
        assert code == CHECK_VIOLATION, f"{code}: {message}"
        assert "ck_ledger_transaction_kind" in message, message

    async def test_an_invented_account_is_refused(
        self, worker: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        subject = await _match_result_with(owner, (ExceptionCode.PRICE_VARIANCE,))
        transaction = worker.transaction()
        await transaction.start()
        try:
            header = await worker.fetchval(
                "insert into ledger_transaction (kind, currency, currency_exponent, "
                "match_result_id) values ('accrual', 'EUR', 2, $1) returning id",
                subject,
            )
            code, message = await _refused(
                worker,
                "insert into ledger_entry (transaction_id, account, amount_minor) "
                "values ($1, 'attacker_slush', 1)",
                header,
            )
        finally:
            await transaction.rollback()
        assert code == CHECK_VIOLATION, f"{code}: {message}"
        assert "ck_ledger_entry_account" in message, message

    async def test_the_worker_cannot_post_an_opening_balance(
        self, worker: asyncpg.Connection
    ) -> None:
        """The one kind that legitimately cites nothing, which is exactly why a
        process that parses hostile documents may not write it."""
        code, message = await _refused(
            worker,
            "insert into ledger_transaction (kind, currency, currency_exponent) "
            "values ('opening', 'EUR', 2)",
        )
        assert (code, NO_POLICY in message) == (INSUFFICIENT_PRIVILEGE, True), f"{code}: {message}"

    async def test_a_transaction_that_cites_its_subject_is_accepted(
        self, worker: asyncpg.Connection, owner: async_sessionmaker[AsyncSession]
    ) -> None:
        """The vacuity guard: constraints that refused everything would pass all
        four tests above."""
        subject = await _match_result_with(owner, (ExceptionCode.PRICE_VARIANCE,))
        transaction = worker.transaction()
        await transaction.start()
        try:
            header = await worker.fetchval(
                "insert into ledger_transaction (kind, currency, currency_exponent, "
                "match_result_id) values ('accrual', 'EUR', 2, $1) returning id",
                subject,
            )
            assert header is not None
        finally:
            await transaction.rollback()
