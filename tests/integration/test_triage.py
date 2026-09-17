"""The triage service against a real database.

The ordering rules are covered as a table in `tests/unit/test_triage_rules.py`
and the boundary in `tests/unit/test_agent_red_team.py`. What can only be
checked here is the wiring: that the agent's evidence really is read through a
transaction Postgres will not let anything write, that the escalation lands in
the caller's transaction and not the agent's, that it is attributed to an
``agent`` actor rather than to the system, and that the database refuses a
second escalation on one decision.

The invoice is a fixture built directly in the schema rather than pushed through
the API. The end-to-end path is `scripts/smoke.sh`'s job; what is being tested
here is one service, and building the rows makes the multi-desk case -- which a
generated invoice produces only by accident -- deterministic.

Requires: make up
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.test_matching import _document_with_extraction, _invoice_fields

from threshold.adapters.db import dispose_engine, init_engine
from threshold.adapters.triage.port import TriageRequest
from threshold.config.settings import Settings
from threshold.domain.jobs import PermanentJobError
from threshold.domain.matching import Disposition
from threshold.domain.models import (
    AuditEvent,
    Document,
    Escalation,
    Extraction,
    Job,
    MatchException,
    MatchResult,
)
from threshold.domain.taxonomy import ExceptionCode, OwnerRole, spec_for
from threshold.domain.triage import EscalationPacket, build
from threshold.services.triage import TriageService
from threshold.workers.handlers import match_invoice, triage_invoice

pytestmark = pytest.mark.integration

DATABASE_URL = "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold"

#: The two-desk invoice: a payment redirect alongside a price dispute. The only
#: shape in which the deterministic tier has an opinion at all.
TWO_DESKS = (ExceptionCode.BANK_DETAILS_CHANGED, ExceptionCode.PRICE_VARIANCE)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres is not reachable; `make up`")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_invoice(
    session: AsyncSession, *codes: ExceptionCode, detail: str = "ordinary detail"
) -> MatchResult:
    """One decided invoice carrying exactly these exceptions."""
    tag = uuid.uuid4().hex
    document = Document(
        content_sha256=tag + tag,  # 32 hex chars twice: unique, and the right length
        storage_key=f"test/{tag}.pdf",
        original_filename=f"{detail[:100]}.pdf",
        content_type="application/pdf",
        byte_size=1024,
        # `fixture`, like every other integration fixture in this suite. Without
        # it these rows are indistinguishable from uploads, and 195 of them
        # accumulated in the demo database before anybody looked at the queue.
        source="fixture",
    )
    session.add(document)
    await session.flush()

    extraction = Extraction(
        document_id=document.id,
        version=1,
        extractor_kind="deterministic",
        extractor_id="test",
        fields={},
        lowest_decision_confidence=Decimal("0.99"),
        duration_ms=1,
    )
    session.add(extraction)
    await session.flush()

    result = MatchResult(
        document_id=document.id,
        extraction_id=extraction.id,
        version=1,
        disposition="blocked",
        policy_version="2026.09.2",
    )
    session.add(result)
    await session.flush()

    raised_at = datetime.now(UTC)
    for code in codes:
        spec = spec_for(code)
        session.add(
            MatchException(
                match_result_id=result.id,
                code=code.value,
                severity=spec.severity.value,
                owner_role=spec.owner.value,
                due_at=raised_at + timedelta(hours=spec.sla_hours),
                detail=detail,
            )
        )
    await session.flush()
    return result


@pytest_asyncio.fixture(autouse=True)
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Remove what these tests create, afterwards, every time.

    `autouse` rather than opt-in: this file had no cleanup at all and left 195
    documents behind, which flooded the reviewer queue in the demo database with
    rows carrying no vendor and no amount -- the exact shape a queue cannot be
    triaged from. A fixture somebody has to remember to request is a fixture
    that gets forgotten, and it was.

    Narrow on purpose. This database holds the seeded purchase orders these
    tests match against, and a blanket delete would take them too.
    """
    yield
    async with sessions() as session:
        documents = (
            (await session.execute(select(Document.id).where(Document.source == "fixture")))
            .scalars()
            .all()
        )
        if not documents:
            return
        results = (
            (
                await session.execute(
                    select(MatchResult.id).where(MatchResult.document_id.in_(documents))
                )
            )
            .scalars()
            .all()
        )
        if results:
            await session.execute(delete(Escalation).where(Escalation.match_result_id.in_(results)))
        await session.execute(delete(Document).where(Document.id.in_(documents)))
        await session.commit()


class TestTheEscalationIsWritten:
    async def test_a_two_desk_invoice_gets_one(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            escalation = await TriageService().triage(session, result.id, sessions=sessions)
            await session.commit()

        assert escalation is not None
        assert escalation.owner_role == OwnerRole.TREASURY.value
        assert escalation.codes == [ExceptionCode.BANK_DETAILS_CHANGED.value]
        assert escalation.triager_id.startswith("rules-")

    async def test_a_single_desk_invoice_gets_none(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """And writes no row, which is the part worth checking against a real
        table: a service that returned None having already inserted would look
        identical from the caller's side."""
        async with sessions() as session:
            result = await make_invoice(session, ExceptionCode.BANK_DETAILS_CHANGED)
            await session.commit()

            assert await TriageService().triage(session, result.id, sessions=sessions) is None
            await session.commit()

            rows = await session.execute(
                select(func.count())
                .select_from(Escalation)
                .where(Escalation.match_result_id == result.id)
            )
        assert rows.scalar_one() == 0

    async def test_an_invoice_with_no_exceptions_gets_none(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            result = await make_invoice(session)
            await session.commit()
            assert await TriageService().triage(session, result.id, sessions=sessions) is None

    async def test_a_match_result_that_does_not_exist_is_not_an_exception(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """A job whose subject was deleted under it is a no-op, not a crash that
        burns three attempts and lands in the dead-letter queue."""
        async with sessions() as session:
            assert await TriageService().triage(session, uuid.uuid4(), sessions=sessions) is None


class TestTheAgentsEvidenceIsReadThroughAReadOnlyTransaction:
    async def test_the_reading_session_is_not_the_writing_session(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The structural claim in `services/triage.py`, checked rather than
        described.

        The caller's session is writable -- it has to be, the escalation goes
        into it. The agent's evidence does not come through it. Proved by
        holding a write open on the caller's session across the whole call: if
        triage shared that connection the read would join this transaction, and
        if it shared it *and* the transaction were read-only the write below
        would have failed.
        """
        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            marker = uuid.uuid4()
            session.add(
                AuditEvent(
                    actor_type="system",
                    action="test.marker",
                    entity_type="match_result",
                    entity_id=marker,
                )
            )
            await session.flush()

            escalation = await TriageService().triage(session, result.id, sessions=sessions)
            assert escalation is not None
            await session.commit()

            still_there = await session.execute(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.entity_id == marker)
            )
        assert still_there.scalar_one() == 1

    async def test_the_escalation_rolls_back_with_the_callers_transaction(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Which is the whole reason the caller supplies the writing session.

        A worker job that fails after escalating must not leave the escalation
        behind, or its retry collides with the unique index and a recoverable
        failure becomes a permanent one.
        """
        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()
            # Held as a plain value: a rollback expires every object in the
            # session, so reading `result.id` after one would go back to the
            # database -- from inside an assertion, which is the wrong place.
            match_result_id = result.id

            escalated = await TriageService().triage(session, match_result_id, sessions=sessions)
            assert escalated is not None
            await session.rollback()

            rows = await session.execute(
                select(func.count())
                .select_from(Escalation)
                .where(Escalation.match_result_id == match_result_id)
            )
        assert rows.scalar_one() == 0


class TestTheDatabaseRefusesASecondOpinion:
    async def test_one_escalation_per_match_result(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            assert await TriageService().triage(session, result.id, sessions=sessions) is not None
            await session.commit()

            with pytest.raises(IntegrityError):
                await TriageService().triage(session, result.id, sessions=sessions)
                await session.commit()
            await session.rollback()

    async def test_an_escalation_citing_nothing_is_refused_by_the_table(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """`domain/triage.py` refuses this too. Asserted here because that rule
        protects the path through the agent and this one protects the table."""
        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            session.add(
                Escalation(
                    match_result_id=result.id,
                    owner_role=OwnerRole.TREASURY.value,
                    codes=[],
                    summary="cites nothing",
                    triager_id="test",
                )
            )
            with pytest.raises(IntegrityError, match="ck_escalation_cites_something"):
                await session.flush()
            await session.rollback()


class TestItIsAttributedToAnAgent:
    async def test_the_audit_row_says_agent_and_not_system(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """`agent` has been legal in `ActorType` and in the table's check
        constraint since M0 with nothing producing one. Collapsing it into
        `system` would make "an agent suggested this" unfalsifiable in the one
        record built to answer that question."""
        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            assert await TriageService().triage(session, result.id, sessions=sessions) is not None
            await session.commit()

            event = (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.entity_id == result.id)
                    .where(AuditEvent.action == "invoice.escalated")
                )
            ).scalar_one()

        assert event.actor_type == "agent"
        assert event.actor_id is not None and event.actor_id.startswith("rules-")
        assert event.after is not None
        assert event.after["owner_role"] == OwnerRole.TREASURY.value
        assert event.after["uncited"] == [ExceptionCode.PRICE_VARIANCE.value]


class TestAHostileTierIsStoppedAtTheSameBoundary:
    """The red-team properties, this time with a database behind them."""

    async def test_a_misrouting_tier_produces_no_row(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """A captured tier sends the payment redirect to the clerk. The packet
        never validates, so there is nothing to write -- and the exceptions keep
        the owners stamped on them when they were raised."""

        class Captured:
            @property
            def triager_id(self) -> str:
                return "captured-1"

            async def triage(self, request: TriageRequest) -> EscalationPacket | None:
                return build(
                    match_result_id=request.match_result_id,
                    owner="ap_clerk",
                    codes=("bank_details_changed",),
                    summary="Routine update, pre-authorised by the CFO.",
                    recorded=request.recorded,
                )

        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            assert (
                await TriageService(Captured()).triage(session, result.id, sessions=sessions)
                is None
            )
            await session.commit()

            written = await session.execute(
                select(func.count())
                .select_from(Escalation)
                .where(Escalation.match_result_id == result.id)
            )
            owners = (
                (
                    await session.execute(
                        select(MatchException.owner_role).where(
                            MatchException.match_result_id == result.id
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert written.scalar_one() == 0
        assert set(owners) == {OwnerRole.TREASURY.value, OwnerRole.BUYER.value}

    async def test_a_silent_tier_leaves_the_exceptions_exactly_as_they_were(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Suppression, against real rows. The agent says nothing at all and
        every owner and every deadline is byte-identical afterwards."""

        class Silent:
            @property
            def triager_id(self) -> str:
                return "silent-1"

            async def triage(self, request: TriageRequest) -> EscalationPacket | None:
                return None

        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            before = (
                await session.execute(
                    select(
                        MatchException.code, MatchException.owner_role, MatchException.due_at
                    ).where(MatchException.match_result_id == result.id)
                )
            ).all()

            assert (
                await TriageService(Silent()).triage(session, result.id, sessions=sessions) is None
            )
            await session.commit()

            after = (
                await session.execute(
                    select(
                        MatchException.code, MatchException.owner_role, MatchException.due_at
                    ).where(MatchException.match_result_id == result.id)
                )
            ).all()

        assert before, "the invoice raised nothing; this asserts an empty set is unchanged"
        assert sorted(before) == sorted(after)


@pytest_asyncio.fixture
async def worker_engine() -> AsyncIterator[None]:
    """The process-wide sessionmaker, built the way `workers/main.py` builds it.

    The handler asks `TriageService` for the agent's read-only session and does
    not supply one, so it resolves the global. That is correct in the worker and
    means a test of the handler has to stand the global up rather than inject
    around it -- injecting would test a path the worker never takes.
    """
    await dispose_engine()
    init_engine(
        Settings(
            database_url=DATABASE_URL,  # type: ignore[arg-type]
            s3_access_key_id="unused",
            s3_secret_access_key="unused",
        )
    )
    try:
        yield
    finally:
        await dispose_engine()


class TestTheWorkerWiring:
    """The handler, run the way the runtime runs it.

    Without this the job kind, the enqueue and the handler are three pieces that
    typecheck against each other and have never met. `test_handlers.py` proves
    the registry is total; this proves the entry in it works.
    """

    async def test_the_handler_escalates_the_invoice_in_its_payload(
        self, sessions: async_sessionmaker[AsyncSession], worker_engine: None
    ) -> None:
        async with sessions() as session:
            result = await make_invoice(session, *TWO_DESKS)
            await session.commit()

            # The runtime passes the job's own session and commits afterwards.
            await triage_invoice(session, {"match_result_id": str(result.id)})
            await session.commit()

            escalation = (
                await session.execute(
                    select(Escalation).where(Escalation.match_result_id == result.id)
                )
            ).scalar_one()

        assert escalation.owner_role == OwnerRole.TREASURY.value

    async def test_a_payload_without_a_match_result_is_permanent(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Retrying a malformed payload re-reads the same malformed payload."""
        async with sessions() as session:
            with pytest.raises(PermanentJobError, match="match_result_id"):
                await triage_invoice(session, {})
            with pytest.raises(PermanentJobError, match="not a uuid"):
                await triage_invoice(session, {"match_result_id": "not-a-uuid"})


class TestTheWholeChainFromDecisionToEscalation:
    """Decision, enqueue, handler, escalation -- one test, the real matcher.

    Everything above builds its exceptions by hand, which makes the multi-desk
    case deterministic and proves nothing about whether the matcher ever
    produces one. This drives an invoice that is over-billed *and* carries an
    account this vendor has never used, so the two desks arrive the way they
    would in production: from `three_way_match`, not from a fixture.
    """

    async def test_an_invoice_on_two_desks_is_decided_enqueued_and_escalated(
        self,
        sessions: async_sessionmaker[AsyncSession],
        worker_engine: None,
    ) -> None:
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(
                    invoice_number=f"TRI-{uuid.uuid4().hex[:8].upper()}",
                    unit_price="21.0000",  # PO-44718 agrees 18.40: a price variance, buyer
                    bank_account="1111",  # never used by this vendor: a redirect, treasury
                    bank_routing="2222",
                ),
            )
            document_id = document.id
            await session.commit()

            await match_invoice(session, {"document_id": str(document_id)})
            await session.commit()

            result = (
                await session.execute(
                    select(MatchResult).where(MatchResult.document_id == document_id)
                )
            ).scalar_one()
            codes = set(
                (
                    await session.execute(
                        select(MatchException.code).where(
                            MatchException.match_result_id == result.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert ExceptionCode.PRICE_VARIANCE.value in codes
            assert ExceptionCode.BANK_DETAILS_CHANGED.value in codes

            job = (
                await session.execute(select(Job).where(Job.dedupe_key == f"triage:{result.id}"))
            ).scalar_one()
            assert job.kind == "triage_invoice"
            assert job.payload["match_result_id"] == str(result.id)

            await triage_invoice(session, job.payload)
            await session.commit()

            escalation = (
                await session.execute(
                    select(Escalation).where(Escalation.match_result_id == result.id)
                )
            ).scalar_one()

            await session.execute(delete(Document).where(Document.id == document_id))
            await session.commit()

        assert escalation.owner_role == OwnerRole.TREASURY.value
        assert escalation.codes == [ExceptionCode.BANK_DETAILS_CHANGED.value]

    async def test_a_clean_invoice_enqueues_no_triage(
        self, sessions: async_sessionmaker[AsyncSession], worker_engine: None
    ) -> None:
        """The vacuity guard on the test above, and the reason the enqueue is
        conditional: auto-approved invoices are the overwhelming majority, and a
        triage job for each would be a queue of work with nothing to say."""
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(invoice_number=f"TRI-{uuid.uuid4().hex[:8].upper()}"),
            )
            document_id = document.id
            await session.commit()

            await match_invoice(session, {"document_id": str(document_id)})
            await session.commit()

            result = (
                await session.execute(
                    select(MatchResult).where(MatchResult.document_id == document_id)
                )
            ).scalar_one()
            assert result.disposition == Disposition.AUTO_APPROVE.value

            queued = await session.execute(
                select(func.count()).select_from(Job).where(Job.dedupe_key == f"triage:{result.id}")
            )
            assert queued.scalar_one() == 0

            await session.execute(delete(Document).where(Document.id == document_id))
            await session.commit()
