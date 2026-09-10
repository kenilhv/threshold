"""The matching service against the real schema and the real seed data.

The matcher itself is covered exhaustively as a table in
``tests/unit/test_matching.py``. What can only be checked here is everything
between an extraction row and a decision row: vendor resolution, finding the
cited purchase order, summing receipts across deliveries, and the versioning
that keeps an earlier decision readable after a later one replaces it.

These run against the application database rather than a private one, because
the purchase orders they match against are the seeded ones -- the same rows the
demo and the smoke test use. Documents are created directly rather than through
the queue, so a live worker cannot race the assertions.

Requires: make up
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from threshold.domain.extraction import RESTRICTED_FIELD_KEYS, redacted_fields
from threshold.domain.matching import Disposition
from threshold.domain.models import (
    AuditEvent,
    Document,
    Extraction,
    MatchException,
    MatchResult,
    Vendor,
)
from threshold.domain.taxonomy import ExceptionCode, OwnerRole
from threshold.services.matching import MatchingError, MatchingService

pytestmark = pytest.mark.integration

DATABASE_URL = "postgresql+asyncpg://threshold:threshold@localhost:5432/threshold"

#: Seeded. PO-44718 carries three lines at 18.40, all fully received.
CLEAN_PO = "PO-44718"
#: Seeded, ordered, nothing received.
UNRECEIVED_PO = "PO-44730"


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


def _field(value: object, confidence: float = 0.95) -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "bbox": None, "raw_text": None}


def _invoice_fields(
    *,
    invoice_number: str,
    bank_account: str | None = "8291",
    bank_routing: str | None = "9593",
    bank_confidence: float = 0.88,
    omit_bank: bool = False,
    po_number: str | None = CLEAN_PO,
    vendor_name: str = "Northgate Supply",
    vendor_tax_id: str = "",
    currency: str = "EUR",
    unit_price: str = "18.4000",
    quantity: str = "12",
    sku: str = "NG-0O10l",
    description: str = "Hex bolt M8x40 zinc plated, DIN 933, boxed 100",
) -> dict[str, Any]:
    """One line, priced to agree with PO-44718 unless a test says otherwise."""
    line_total = str(Decimal(quantity) * Decimal(unit_price))
    subtotal = line_total
    tax = str((Decimal(subtotal) * Decimal("0.19")).quantize(Decimal("0.0001")))
    total = str(Decimal(subtotal) + Decimal(tax))
    bank: dict[str, Any] = {}
    if not omit_bank:
        bank = {
            "bank_account_last4": _field(bank_account, bank_confidence),
            "bank_routing_last4": _field(bank_routing, bank_confidence),
        }
    return {
        **bank,
        "document_type": _field("invoice"),
        "invoice_number": _field(invoice_number),
        "vendor_name": _field(vendor_name),
        "vendor_tax_id": _field(vendor_tax_id),
        "po_number": _field(po_number),
        "issue_date": _field("2026-07-14"),
        "due_date": _field("2026-08-13"),
        "currency": _field(currency),
        "subtotal": _field(subtotal),
        "tax_amount": _field(tax),
        "total": _field(total),
        "line_items": [
            {
                "description": _field(description),
                "quantity": _field(quantity),
                "unit_price": _field(unit_price),
                "line_total": _field(line_total),
                "sku": _field(sku),
            }
        ],
    }


async def _document_with_extraction(
    session: AsyncSession, fields: dict[str, Any], *, confidence: str = "0.95"
) -> Document:
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    document = Document(
        content_sha256=digest,
        storage_key=f"documents/{digest[:2]}/{digest[2:4]}/{digest}.pdf",
        original_filename="fixture.pdf",
        content_type="application/pdf",
        byte_size=1024,
        source="fixture",
    )
    session.add(document)
    await session.flush()

    session.add(
        Extraction(
            document_id=document.id,
            version=1,
            extractor_kind="deterministic",
            extractor_id="test",
            fields=fields,
            lowest_decision_confidence=Decimal(confidence),
            duration_ms=1,
        )
    )
    await session.flush()
    return document


@pytest_asyncio.fixture
async def cleanup(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Remove only what these tests create.

    Deliberately narrow: this database holds the seed, and a blanket delete
    would take the purchase orders these tests exist to match against.
    """
    yield
    async with sessions() as session:
        docs = (
            (await session.execute(select(Document.id).where(Document.source == "fixture")))
            .scalars()
            .all()
        )
        if docs:
            await session.execute(delete(Document).where(Document.id.in_(docs)))
        await session.execute(delete(Vendor).where(Vendor.vendor_number.like("V-TEST-%")))
        await session.commit()


class TestAgainstSeededOrders:
    async def test_an_invoice_that_agrees_with_its_order_is_approved(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        async with sessions() as session:
            document = await _document_with_extraction(
                session, _invoice_fields(invoice_number=f"OK-{uuid.uuid4().hex[:8]}")
            )
            result = await MatchingService().match_document(session, document.id)
            await session.commit()

        assert result.disposition == Disposition.AUTO_APPROVE.value
        assert result.version == 1
        assert result.purchase_order_id is not None, "the cited order should have been found"
        assert result.line_pairs == [{"invoice_line": 1, "po_line": 1}]
        assert result.skipped_checks == []

    async def test_billing_above_the_agreed_price_blocks_and_routes_to_the_buyer(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(invoice_number=f"HI-{uuid.uuid4().hex[:8]}", unit_price="21.0000"),
            )
            result = await MatchingService().match_document(session, document.id)
            await session.flush()
            raised = (
                (
                    await session.execute(
                        select(MatchException).where(MatchException.match_result_id == result.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        assert result.disposition == Disposition.BLOCKED.value
        assert [e.code for e in raised] == [ExceptionCode.PRICE_VARIANCE.value]
        assert raised[0].owner_role == OwnerRole.BUYER.value
        assert raised[0].due_at > datetime.now(UTC), "the clock starts when it is raised"
        assert raised[0].breach is not None
        assert raised[0].breach["expected"] == "18.4000"
        assert raised[0].breach["variance"] == "2.6000"

    async def test_billing_before_receipt_reviews_and_routes_to_receiving(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """The DQ / DW distinction, end to end.

        Identical arithmetic to over-billing, and it must not reach the same
        desk or carry the same severity.
        """
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(
                    invoice_number=f"EARLY-{uuid.uuid4().hex[:8]}",
                    po_number=UNRECEIVED_PO,
                    sku="NG-4420k",
                    description="Anchor bolt M12x120 hot-dip galvanised, boxed 25",
                    quantity="8",
                    unit_price="31.5000",
                ),
            )
            result = await MatchingService().match_document(session, document.id)
            await session.flush()
            raised = (
                (
                    await session.execute(
                        select(MatchException).where(MatchException.match_result_id == result.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        assert result.disposition == Disposition.NEEDS_REVIEW.value
        assert [e.code for e in raised] == [ExceptionCode.RECEIPT_MISSING.value]
        assert raised[0].owner_role == OwnerRole.RECEIVING.value

    async def test_an_order_that_does_not_exist_blocks(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(invoice_number=f"NOPO-{uuid.uuid4().hex[:8]}", po_number="PO-0000"),
            )
            result = await MatchingService().match_document(session, document.id)
            await session.commit()

        assert result.disposition == Disposition.BLOCKED.value
        assert result.purchase_order_id is None
        assert result.skipped_checks, "not matching lines must be recorded, not implied"


class TestVendorResolution:
    async def test_a_tax_identifier_resolves_ahead_of_a_name(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """A government-issued identifier beats a name typed by a supplier."""
        async with sessions() as session:
            session.add(
                Vendor(
                    vendor_number=f"V-TEST-{uuid.uuid4().hex[:6]}",
                    legal_name="Kestrel Components AB",
                    display_name="Kestrel Components",
                    country_code="SE",
                    default_currency="EUR",
                    tax_id="SE556677889901",
                )
            )
            await session.flush()

            document = await _document_with_extraction(
                session,
                _invoice_fields(
                    invoice_number=f"TAX-{uuid.uuid4().hex[:8]}",
                    vendor_name="a name that matches nothing at all",
                    vendor_tax_id="SE556677889901",
                    po_number=None,
                ),
            )
            result = await MatchingService().match_document(session, document.id)
            await session.flush()
            codes = {
                e.code
                for e in (
                    (
                        await session.execute(
                            select(MatchException).where(
                                MatchException.match_result_id == result.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            await session.commit()

        assert ExceptionCode.VENDOR_UNKNOWN.value not in codes, (
            "the tax identifier should have resolved the vendor despite the unusable name"
        )
        assert ExceptionCode.NO_PO_REFERENCE.value in codes

    async def test_a_vendor_on_hold_blocks_however_clean_the_invoice(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """Pinewood is seeded on hold precisely so this is checkable."""
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(
                    invoice_number=f"HOLD-{uuid.uuid4().hex[:8]}",
                    vendor_name="Pinewood Freight",
                    po_number=None,
                ),
            )
            result = await MatchingService().match_document(session, document.id)
            await session.flush()
            codes = {
                e.code
                for e in (
                    (
                        await session.execute(
                            select(MatchException).where(
                                MatchException.match_result_id == result.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            await session.commit()

        assert ExceptionCode.VENDOR_ON_HOLD.value in codes
        assert result.disposition == Disposition.BLOCKED.value


class TestDuplicatesAndVersioning:
    async def test_a_second_invoice_under_the_same_number_is_suspected(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        number = f"DUP-{uuid.uuid4().hex[:8]}"
        async with sessions() as session:
            first = await _document_with_extraction(session, _invoice_fields(invoice_number=number))
            await MatchingService().match_document(session, first.id)
            await session.commit()

        async with sessions() as session:
            second = await _document_with_extraction(
                session, _invoice_fields(invoice_number=number)
            )
            result = await MatchingService().match_document(session, second.id)
            await session.flush()
            codes = {
                e.code
                for e in (
                    (
                        await session.execute(
                            select(MatchException).where(
                                MatchException.match_result_id == result.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            await session.commit()

        assert ExceptionCode.DUPLICATE_SUSPECTED.value in codes
        assert result.disposition == Disposition.BLOCKED.value

    async def test_rematching_writes_a_new_version_and_keeps_the_old_one(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """A decision is a record of a moment, so a later one must not erase it."""
        async with sessions() as session:
            document = await _document_with_extraction(
                session, _invoice_fields(invoice_number=f"RE-{uuid.uuid4().hex[:8]}")
            )
            service = MatchingService()
            first = await service.match_document(session, document.id)
            second = await service.match_document(session, document.id)
            await session.flush()

            versions = (
                (
                    await session.execute(
                        select(MatchResult.version)
                        .where(MatchResult.document_id == document.id)
                        .order_by(MatchResult.version)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        assert first.version == 1
        assert second.version == 2
        assert list(versions) == [1, 2], "the earlier decision must still be readable"


class TestPreconditions:
    async def test_a_document_with_no_extraction_cannot_be_matched(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """Permanent, not transient: no retry grows the document an extraction."""
        async with sessions() as session:
            digest = uuid.uuid4().hex + uuid.uuid4().hex
            document = Document(
                content_sha256=digest,
                storage_key=f"documents/{digest[:2]}/{digest[2:4]}/{digest}.pdf",
                original_filename="bare.pdf",
                content_type="application/pdf",
                byte_size=10,
                source="fixture",
            )
            session.add(document)
            await session.flush()

            with pytest.raises(MatchingError, match="no extraction"):
                await MatchingService().match_document(session, document.id)
            await session.rollback()

    async def test_an_unknown_document_is_refused(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        async with sessions() as session:
            with pytest.raises(MatchingError, match="no such document"):
                await MatchingService().match_document(session, uuid.uuid4())


class TestPaymentDetails:
    """The flagship control, against real vendor bank history.

    Northgate (V-100418) is seeded with two rows: account 8291 / routing 9593
    currently effective, and account 0073 / routing 0248 retired forty days
    before SEED_EPOCH. That history is what makes "changed since we last paid
    them" answerable rather than merely "here is the current account".
    """

    async def _run(
        self, sessions: async_sessionmaker[AsyncSession], fields: dict[str, Any]
    ) -> tuple[list[MatchException], list[str], MatchResult]:
        async with sessions() as session:
            document = await _document_with_extraction(session, fields)
            result = await MatchingService().match_document(session, document.id)
            await session.flush()
            raised = list(
                (
                    await session.execute(
                        select(MatchException).where(MatchException.match_result_id == result.id)
                    )
                )
                .scalars()
                .all()
            )
            skips = list(result.skipped_checks)
            disposition = result.disposition
            await session.commit()
        result.disposition = disposition
        return raised, skips, result

    @staticmethod
    def _bank(raised: list[MatchException]) -> list[MatchException]:
        return [e for e in raised if e.code == ExceptionCode.BANK_DETAILS_CHANGED.value]

    async def test_the_current_account_matches_and_records_no_skip(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        raised, skips, result = await self._run(
            sessions, _invoice_fields(invoice_number=f"BANKOK-{uuid.uuid4().hex[:8]}")
        )
        assert not self._bank(raised)
        assert not [s for s in skips if s.startswith("payment-detail")]
        assert result.disposition == Disposition.AUTO_APPROVE.value

    async def test_an_account_we_have_never_held_blocks_to_treasury(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """The redirect shape: details matching nothing in this vendor's history."""
        raised, _, result = await self._run(
            sessions,
            _invoice_fields(
                invoice_number=f"REDIRECT-{uuid.uuid4().hex[:8]}",
                bank_account="4417",
                bank_routing="7781",
            ),
        )
        bank = self._bank(raised)
        assert len(bank) == 1
        assert bank[0].owner_role == OwnerRole.TREASURY.value
        assert bank[0].severity == "block"
        assert "differ from those last used" in bank[0].detail
        assert not any(ch.isdigit() for ch in bank[0].detail), (
            "the detail is shown to reviewers; the digits themselves are RESTRICTED"
        )
        assert result.disposition == Disposition.BLOCKED.value

    async def test_a_retired_account_is_named_as_replaced_not_as_unknown(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """Stale or replayed, rather than a redirect. Same desk, different call."""
        raised, _, result = await self._run(
            sessions,
            _invoice_fields(
                invoice_number=f"STALE-{uuid.uuid4().hex[:8]}",
                bank_account="0073",
                bank_routing="0248",
            ),
        )
        bank = self._bank(raised)
        assert len(bank) == 1
        assert "since replaced" in bank[0].detail
        assert result.disposition == Disposition.BLOCKED.value

    async def test_a_routing_change_alone_is_still_a_change(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """There is no immaterial fraction of a payment instruction."""
        raised, _, _ = await self._run(
            sessions,
            _invoice_fields(invoice_number=f"ROUTE-{uuid.uuid4().hex[:8]}", bank_routing="0248"),
        )
        assert len(self._bank(raised)) == 1

    async def test_a_vendor_with_no_payment_details_on_file_is_not_accused(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """Calder and Roe is seeded active with no bank row.

        Every genuine first invoice from a new supplier looks exactly like this.
        Raising the highest-severity code on it would be a false positive by
        construction, so it is recorded as not compared instead.
        """
        raised, skips, _ = await self._run(
            sessions,
            _invoice_fields(
                invoice_number=f"NOHIST-{uuid.uuid4().hex[:8]}",
                vendor_name="Calder and Roe",
                po_number=None,
            ),
        )
        assert not self._bank(raised)
        assert any("nothing this invoice could have changed from" in s for s in skips)

    async def test_an_unreadable_payment_line_is_recorded_not_ignored(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        raised, skips, _ = await self._run(
            sessions,
            _invoice_fields(
                invoice_number=f"UNREAD-{uuid.uuid4().hex[:8]}",
                bank_account=None,
                bank_routing=None,
            ),
        )
        assert not self._bank(raised)
        assert any("could be read from the page" in s for s in skips)

    async def test_a_low_confidence_read_does_not_accuse_anyone(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """Below the read floor is "we did not read it", not "it changed"."""
        raised, skips, _ = await self._run(
            sessions,
            _invoice_fields(
                invoice_number=f"FAINT-{uuid.uuid4().hex[:8]}",
                bank_account="4417",
                bank_routing="7781",
                bank_confidence=0.30,
            ),
        )
        assert not self._bank(raised)
        assert any("could be read from the page" in s for s in skips)

    async def test_a_reading_written_before_the_feature_is_distinguishable(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """A pre-1.1.0 JSONB row must not read as "asked and found nothing".

        Different fact, different remediation: this one is fixed by
        re-extracting, and that is only knowable because the field is absent
        rather than defaulted to a not-found instance.
        """
        raised, skips, _ = await self._run(
            sessions,
            _invoice_fields(invoice_number=f"OLDROW-{uuid.uuid4().hex[:8]}", omit_bank=True),
        )
        assert not self._bank(raised)
        assert any("predates payment-detail extraction" in s for s in skips)

    async def test_the_review_projection_never_carries_the_payment_digits(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """Dropped, not masked. Four digits is a short enough candidate list."""
        fields = _invoice_fields(invoice_number=f"REDACT-{uuid.uuid4().hex[:8]}")
        assert set(fields) >= RESTRICTED_FIELD_KEYS, "the fixture should carry them"
        projected = redacted_fields(fields)
        assert not (RESTRICTED_FIELD_KEYS & set(projected))
        assert "invoice_number" in projected, "redaction must not gut the projection"


class TestTheDecisionRecordsItself:
    """`match.decided` is written in the same transaction as the rows it describes.

    models.py has claimed since M0 that every state transition writes an audit
    event. The most consequential transition in the system — deciding whether an
    invoice may be paid without a human — emitted a structlog line and nothing
    else, so the claim was false exactly where it mattered most.
    """

    async def test_a_decision_writes_one_audit_row(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        async with sessions() as session:
            document = await _document_with_extraction(
                session, _invoice_fields(invoice_number=f"AUDIT-{uuid.uuid4().hex[:8]}")
            )
            result = await MatchingService().match_document(session, document.id)
            await session.flush()

            events = (
                (
                    await session.execute(
                        select(AuditEvent)
                        .where(AuditEvent.entity_id == result.id)
                        .where(AuditEvent.action == "match.decided")
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        assert len(events) == 1
        after = events[0].after or {}
        assert events[0].actor_type == "system"
        assert events[0].entity_type == "match_result"
        assert after["disposition"] == Disposition.AUTO_APPROVE.value
        assert after["policy_version"] == "2026.09.1"
        assert after["exception_codes"] == []

    async def test_the_row_lists_the_codes_that_blocked_it(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """So "why did this block" is answerable from the audit trail alone."""
        async with sessions() as session:
            document = await _document_with_extraction(
                session,
                _invoice_fields(
                    invoice_number=f"AUDITX-{uuid.uuid4().hex[:8]}", unit_price="21.0000"
                ),
            )
            result = await MatchingService().match_document(session, document.id)
            await session.flush()
            event = (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.entity_id == result.id)
                    .where(AuditEvent.action == "match.decided")
                )
            ).scalar_one()
            after = event.after or {}
            await session.commit()

        assert after["disposition"] == Disposition.BLOCKED.value
        assert after["exception_codes"] == [ExceptionCode.PRICE_VARIANCE.value]

    async def test_the_audit_row_cannot_be_edited_afterwards(
        self, sessions: async_sessionmaker[AsyncSession], cleanup: None
    ) -> None:
        """The append-only trigger, applied to the new call site."""
        async with sessions() as session:
            document = await _document_with_extraction(
                session, _invoice_fields(invoice_number=f"IMMUT-{uuid.uuid4().hex[:8]}")
            )
            result = await MatchingService().match_document(session, document.id)
            await session.commit()

        async with sessions() as session:
            with pytest.raises(Exception, match=r"append-only|restrict|cannot"):
                await session.execute(
                    sa_text(
                        "UPDATE audit_event SET action = 'tampered' "
                        "WHERE entity_id = :entity AND action = 'match.decided'"
                    ),
                    {"entity": result.id},
                )
                await session.commit()
            await session.rollback()
