"""The metrics a real decision produces, and the labels a real database attaches.

The unit suite proves the registry is enforced and that the HTTP labels are
bounded. What it cannot prove is that the instruments are wired into the code
paths that matter, because those paths need a database: a match needs a purchase
order to judge against, and the queue gauges need a queue.

Two of the checks here are about labels this project does not write. The
SQLAlchemy instrumentation attaches the connection pool's DSN to every data
point, and the only thing keeping the database password out of a metrics backend
is that SQLAlchemy strips the userinfo before using the URL as a name. That is a
property of a dependency, so it is pinned rather than trusted.

Requires: make up
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.test_matching import (
    CLEAN_PO,
    DATABASE_URL,
    UNRECEIVED_PO,
    _document_with_extraction,
    _invoice_fields,
    cleanup,  # noqa: F401  -- a fixture, used by name
)
from tests.metrics_reader import Recording

from threshold.adapters.db import dispose_engine, init_engine, session_scope
from threshold.adapters.queue import PostgresJobQueue
from threshold.config.settings import Settings
from threshold.domain.jobs import JobState
from threshold.domain.matching import Disposition
from threshold.domain.taxonomy import ExceptionCode
from threshold.observability import metrics as m
from threshold.observability.tracing import configure_tracing
from threshold.services.matching import MatchingService

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres is not reachable; start the stack with `make up`")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestTheDecisionIsCounted:
    """The metric this project exists to produce.

    "What share of invoices clear without a person" is the question an AP
    control plane is bought to answer, and it is a ratio between two labels of
    one counter rather than a comparison between two different ones.
    """

    async def test_an_auto_approval_counts_as_a_decision_and_as_the_machine(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cleanup: None,  # noqa: F811
        recording: Recording,
    ) -> None:
        async with sessions() as session:
            document = await _document_with_extraction(
                session, _invoice_fields(invoice_number=f"MET-{uuid.uuid4().hex[:8]}")
            )
            result = await MatchingService().match_document(session, document.id)
            await session.commit()

        assert result.disposition == Disposition.AUTO_APPROVE.value

        assert (
            recording.increase(
                m.MATCH_DECISIONS,
                disposition=Disposition.AUTO_APPROVE.value,
                policy_version=result.policy_version,
            )
            == 1
        )
        # The machine writes its decision into the same table a person writes
        # to, so it lands on the same counter under a different actor_type.
        assert (
            recording.increase(m.APPROVAL_DECISIONS, decision="auto_approved", actor_type="system")
            == 1
        )

    async def test_a_blocked_invoice_counts_its_exceptions_by_code(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cleanup: None,  # noqa: F811
        recording: Recording,
    ) -> None:
        """Counted by code, so the dashboard can answer "which control fires most".

        That is the question that tells an operator where the process is
        actually broken, rather than that it is. The taxonomy is frozen, so the
        label is bounded by an enum rather than by hope.

        Which codes this fixture raises is deliberately not written down here.
        The assertion compares the counter against the rows the matcher wrote,
        so it stays true when the tolerance policy changes and it fails if a
        code stops being counted -- neither of which a hard-coded code would do.
        """
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(
                    invoice_number=f"MET-{uuid.uuid4().hex[:8]}", po_number=UNRECEIVED_PO
                ),
            )
            result = await MatchingService().match_document(session, document.id)
            await session.commit()

        async with sessions() as session:
            persisted = {
                row[0]
                for row in await session.execute(
                    text("select code from match_exception where match_result_id = :id"),
                    {"id": result.id},
                )
            }

        assert result.disposition != Disposition.AUTO_APPROVE.value
        assert persisted, "the invoice raised no exceptions, so this proves nothing"

        counted = {a["code"] for a in recording.labels(m.MATCH_EXCEPTIONS)}
        # Tied to the rows rather than to a code named in advance: the metric is
        # only worth having if it says the same thing the database does, and an
        # assertion naming one code would keep passing if the others stopped
        # being counted.
        assert counted == persisted
        assert counted <= {code.value for code in ExceptionCode}, (
            f"an exception code outside the frozen taxonomy reached a label: {counted}"
        )

    async def test_no_decision_is_counted_when_nothing_was_decided(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cleanup: None,  # noqa: F811
        recording: Recording,
    ) -> None:
        """A match that could not be attempted is not a disposition.

        `MatchingError` means there was nothing to judge. Counting it would put
        a failure of the pipeline into the same series as a judgement about an
        invoice, and the automation rate would move for a reason that has
        nothing to do with invoices.
        """
        from threshold.services.matching import MatchingError

        async with sessions() as session:
            with pytest.raises(MatchingError):
                await MatchingService().match_document(session, uuid.uuid4())

        assert m.MATCH_DECISIONS not in recording.names()


class TestTheQueueGaugesReadARealQueue:
    async def test_depth_and_age_come_back_from_postgres(
        self, sessions: async_sessionmaker[AsyncSession], recording: Recording
    ) -> None:
        """Exercises the SQL, which is the part a unit test cannot.

        `oldest_claimable_age` subtracts inside the database on purpose, and the
        expression is the kind that type-checks and then fails at runtime.
        """
        queue = PostgresJobQueue()
        async with sessions() as session:
            stats = await queue.stats(session)
            age = await queue.oldest_claimable_age(session)

        assert age is None or age >= 0
        m.publish_queue_snapshot(
            depth_by_state=stats,
            oldest_pending_age_seconds=age,
            dead_letters_unreplayed=stats.get("dead_letters_unreplayed", 0),
        )

        reported = {a["state"] for a in recording.labels(m.QUEUE_DEPTH)}
        assert reported == {state.value for state in JobState}

    async def test_the_dead_letter_count_matches_the_table(
        self, sessions: async_sessionmaker[AsyncSession], recording: Recording
    ) -> None:
        queue = PostgresJobQueue()
        async with sessions() as session:
            stats = await queue.stats(session)
            m.publish_queue_snapshot(
                depth_by_state=stats,
                oldest_pending_age_seconds=None,
                dead_letters_unreplayed=stats["dead_letters_unreplayed"],
            )
            unreplayed = (
                await session.execute(
                    text("select count(*) from dead_letter where replayed_at is null")
                )
            ).scalar_one()

        assert recording.value(m.DEAD_LETTERS) == unreplayed


@pytest_asyncio.fixture
async def production_engine() -> AsyncIterator[SimpleSpanProcessor]:
    """The engine built exactly the way the API and the worker build it.

    `init_engine` is what production calls, and the point of these tests is that
    what production calls is instrumented. Building an engine by hand in the
    test would prove something about SQLAlchemy and nothing about this service.
    """
    settings = Settings(
        database_url=DATABASE_URL,  # type: ignore[arg-type]
        redis_url="redis://localhost:6379/0",  # type: ignore[arg-type]
        s3_access_key_id="unused",
        s3_secret_access_key="unused",
    )
    # The same two calls, in the same order, that `workers/main.py` makes.
    configure_tracing(settings)
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        "configure_tracing did not install an SDK provider, so no span assertion "
        "below could mean anything"
    )

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    await dispose_engine()
    # `instrument()` carries a global "already instrumented" flag, so a second
    # call is a warning and a no-op -- which would leave the engine built below
    # uninstrumented and make every assertion here a false negative. Production
    # never reaches this: `init_engine` builds one engine per process and
    # `dispose_engine` runs at shutdown. A test that rebuilds it per case does.
    instrumentor = SQLAlchemyInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()

    init_engine(settings)
    exporter.clear()
    try:
        yield exporter
    finally:
        await dispose_engine()
        processor.shutdown()


class TestTheInstrumentationReachesTheEngine:
    """The wiring both the pool metric and every SQL span depend on.

    `SQLAlchemyInstrumentor().instrument()` with no engine works by replacing
    `create_async_engine` on `sqlalchemy.ext.asyncio`. `adapters/db.py` binds
    that name at import, which happens when `api/main.py` is imported --- long
    before `create_app()` runs anything. So the global call reached nothing,
    silently: the service emitted a `connect` span and no `select` spans at all
    from M5 until this was measured, and the pool metric was declared in the
    registry and never produced.

    These tests exist because that failure had no symptom. Nothing raised,
    nothing logged, and the only evidence was an absence in a backend nobody
    was running yet.
    """

    async def test_a_query_produces_a_span(self, production_engine: InMemorySpanExporter) -> None:
        async with session_scope() as session:
            await session.execute(text("select 1"))

        names = [span.name for span in production_engine.get_finished_spans()]
        assert "select" in names, (
            f"the engine is not instrumented; spans were {names}. A 'connect' span "
            "with no 'select' is the exact signature of the import-order failure."
        )

    async def test_the_pool_metric_is_produced(
        self, production_engine: InMemorySpanExporter, recording: Recording
    ) -> None:
        async with session_scope() as session:
            await session.execute(text("select 1"))

        # Presence, not movement. The pool returns the connection before this
        # reads, so `used` can end where it started; the question is whether the
        # instrument exists at all, which for three milestones it did not.
        emitted = {name for name, _ in recording.every_point()}
        assert m.DB_CONNECTIONS in emitted, (
            "the connection pool metric is declared in REGISTRY and was not emitted"
        )

    async def test_the_pool_name_carries_no_credentials(
        self, production_engine: InMemorySpanExporter, recording: Recording
    ) -> None:
        """The one label in the registry this project does not author.

        SQLAlchemy's instrumentation names the pool after the DSN. It strips the
        userinfo first, which is the only reason the database password is not
        being written into every metrics backend this connects to. Pinned here
        so an upgrade that changes it fails the gate rather than the company.
        """
        async with session_scope() as session:
            await session.execute(text("select 1"))

        pools = {
            attributes.get("pool.name")
            for name, attributes in recording.every_point()
            if name == m.DB_CONNECTIONS
        }
        assert pools, "the pool metric was never emitted, so this proves nothing"
        for pool in pools:
            assert isinstance(pool, str)
            assert "threshold:threshold@" not in pool, f"credentials in a label: {pool}"
            assert "@" not in pool, f"a userinfo section survived: {pool}"


class TestTheRegistryCoversWhatIsActuallyEmitted:
    async def test_nothing_undeclared_is_exported(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cleanup: None,  # noqa: F811
        recording: Recording,
    ) -> None:
        """The registry has to cover everything, including what dependencies add.

        A new instrumentation package, or a new instrument somebody adds without
        declaring it, arrives here rather than in a bill.
        """
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(invoice_number=f"MET-{uuid.uuid4().hex[:8]}", po_number=CLEAN_PO),
            )
            await MatchingService().match_document(session, document.id)
            await session.commit()

        points = list(recording.every_point())
        assert points, "the exposition is empty, so this checks nothing"
        for name, attributes in points:
            spec = m.BY_NAME.get(name)
            assert spec is not None, (
                f"{name} is exported but not declared in REGISTRY; declare it with the "
                "labels it may carry, or drop it"
            )
            assert set(attributes) <= set(spec.labels), (
                f"{name} carries undeclared keys {set(attributes) - set(spec.labels)}"
            )
