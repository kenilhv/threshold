"""Two reviewers, one invoice, and what the loser is told.

`uq_approval_decision_terminal` has always made the outcome correct: one
terminal decision per match result, arbitrated by the database rather than by a
read. What was wrong was the sentence the loser got.

`decide` checks for an existing decision, then inserts. Between those two
statements another transaction can commit, and the insert then raises
`IntegrityError`. The handler caught it and raised
`AlreadyDecidedError(decision)` -- passing the row that had *just failed to
insert*, which is the loser's own. So a clerk who tried to reject an invoice
somebody else had approved was told "this invoice was already rejected by
<themselves>". Both the verb and the name were wrong, in a message about
whether money moves.

That is only reachable by racing two real transactions, which is what this file
does. `test_approvals.py` covers the sequential case over HTTP, where the
pre-flight check catches it and the message was always right.

Requires: make up
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.integration.databases import ISOLATED
from tests.integration.test_matching import _document_with_extraction, _invoice_fields

from threshold.domain.approval import Decision
from threshold.domain.models import ApprovalDecision, Document, MatchException, MatchResult
from threshold.domain.roles import Role
from threshold.domain.taxonomy import ExceptionCode, spec_for
from threshold.services.approvals import AlreadyDecidedError, ApprovalService, DecisionRequest

pytestmark = pytest.mark.integration

#: Isolated: two concurrent transactions against one row, with nothing else
#: deciding anything.
DATABASE_URL = ISOLATED


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


@pytest_asyncio.fixture
async def snapshot_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on a REPEATABLE READ snapshot, which is what makes this race reachable.

    Postgres defaults to READ COMMITTED, where every statement takes a fresh
    snapshot -- so the loser's pre-flight `SELECT` *does* see the winner's
    committed row, the early check catches it, and the insert never runs. The
    first version of this file did exactly that and passed with the bug
    reintroduced: it was asserting about the pre-flight path, which was always
    correct.

    REPEATABLE READ pins the loser's snapshot at his first statement. His
    pre-flight then sees nothing, he proceeds, and the unique index refuses the
    insert -- which is the only path on which `AlreadyDecidedError` was
    constructed from the wrong row.

    This is not an exotic configuration standing in for a real one. It is the
    same window READ COMMITTED has between the check and the insert, held open
    long enough to test.
    """
    engine = create_async_engine(DATABASE_URL, isolation_level="REPEATABLE READ")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _contested_invoice(session: AsyncSession) -> MatchResult:
    """One invoice blocked on a price variance, which the buyer owns."""
    document = await _document_with_extraction(
        session,
        _invoice_fields(
            invoice_number=f"RACE-{uuid.uuid4().hex[:8].upper()}",
            unit_price="21.0000",  # PO-44718 agrees 18.40
        ),
    )
    await session.flush()

    result = MatchResult(
        document_id=document.id,
        extraction_id=(
            await session.execute(
                text("select id from extraction where document_id = :d"), {"d": document.id}
            )
        ).scalar_one(),
        version=1,
        disposition="blocked",
        policy_version="2026.09.2",
    )
    session.add(result)
    await session.flush()

    spec = spec_for(ExceptionCode.PRICE_VARIANCE)
    session.add(
        MatchException(
            match_result_id=result.id,
            code=ExceptionCode.PRICE_VARIANCE.value,
            severity=spec.severity.value,
            owner_role=spec.owner.value,
            due_at=datetime.now(UTC) + timedelta(hours=spec.sla_hours),
            detail="billed above the agreed price",
        )
    )
    await session.flush()
    return result


@pytest_asyncio.fixture(autouse=True)
async def clean(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    async with sessions() as session:
        documents = (
            (
                await session.execute(
                    select(Document.id).where(Document.original_filename.like("fixture%"))
                )
            )
            .scalars()
            .all()
        )
        if documents:
            await session.execute(delete(Document).where(Document.id.in_(documents)))
            await session.commit()


class TestTheLoserIsToldWhatActuallyHappened:
    async def test_the_refusal_names_the_winning_decision_and_not_the_loser_s(
        self,
        sessions: async_sessionmaker[AsyncSession],
        snapshot_sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        """The bug, as a scenario.

        Two buyers open the same invoice. One approves. The other, a moment
        later and having already passed the pre-flight check, tries to reject.
        The database refuses the second write -- and the second buyer must be
        told it was *approved*, by the *other person*.
        """
        async with sessions() as alice, snapshot_sessions() as bob:
            result = await _contested_invoice(alice)
            await alice.commit()
            match_result_id = result.id

            service = ApprovalService()

            # Bob reads first, which pins his snapshot. Nothing is decided yet,
            # so his pre-flight check will pass even after Alice commits.
            await bob.execute(
                select(ApprovalDecision).where(ApprovalDecision.match_result_id == match_result_id)
            )

            # Alice approves and commits underneath him.
            await service.decide(
                alice,
                DecisionRequest(
                    match_result_id=match_result_id,
                    decision=Decision.APPROVED,
                    seen_version=1,
                    reason="agreed by phone",
                ),
                actor_id="alice-sub",
                actor_label="Alice",
                roles=frozenset({Role.BUYER}),
            )
            await alice.commit()

            # Bob's write now loses to the unique index.
            with pytest.raises(AlreadyDecidedError) as refused:
                await service.decide(
                    bob,
                    DecisionRequest(
                        match_result_id=match_result_id,
                        decision=Decision.REJECTED,
                        seen_version=1,
                        reason="price is wrong",
                    ),
                    actor_id="bob-sub",
                    actor_label="Bob",
                    roles=frozenset({Role.BUYER}),
                )

            message = str(refused.value)
            # What holds under any isolation level: it never reports the
            # loser's own decision as the one that won. That is the whole bug.
            assert "rejected" not in message, f"it reported the loser's own verb: {message}"
            assert "Bob" not in message, f"it told Bob he had decided it himself: {message}"
            assert "already decided" in message, message
            # And it says plainly that it cannot name the winner, rather than
            # inventing one. Bob's snapshot predates Alice's commit, so her row
            # is genuinely invisible to him -- there is no honest name to give.
            assert refused.value.existing is None
            assert "cannot say who" in message, message

            await bob.rollback()

    async def test_only_one_decision_survives(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The vacuity guard, and the property that was never broken.

        If the second write had succeeded there would be nothing to name and
        the test above would be asserting about an error that never fired.
        """
        async with sessions() as alice, sessions() as bob:
            result = await _contested_invoice(alice)
            await alice.commit()
            match_result_id = result.id
            service = ApprovalService()

            await service.decide(
                alice,
                DecisionRequest(
                    match_result_id=match_result_id,
                    decision=Decision.APPROVED,
                    seen_version=1,
                    reason="agreed by phone",
                ),
                actor_id="alice-sub",
                actor_label="Alice",
                roles=frozenset({Role.BUYER}),
            )
            await alice.commit()

            with pytest.raises(AlreadyDecidedError):
                await service.decide(
                    bob,
                    DecisionRequest(
                        match_result_id=match_result_id,
                        decision=Decision.REJECTED,
                        seen_version=1,
                        reason="price is wrong",
                    ),
                    actor_id="bob-sub",
                    actor_label="Bob",
                    roles=frozenset({Role.BUYER}),
                )
            await bob.rollback()

            surviving = await session_decisions(alice, match_result_id)
        assert [d.decision for d in surviving] == ["approved"]
        assert [d.actor_label for d in surviving] == ["Alice"]


async def session_decisions(
    session: AsyncSession, match_result_id: uuid.UUID
) -> list[ApprovalDecision]:
    return list(
        (
            await session.execute(
                select(ApprovalDecision).where(ApprovalDecision.match_result_id == match_result_id)
            )
        )
        .scalars()
        .all()
    )
