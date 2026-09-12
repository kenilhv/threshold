"""Turning a stored extraction into a decision.

The matcher itself is pure and lives in ``domain/matching.py``. Everything here
is the part that needs a database: resolving which vendor an invoice is from,
finding the purchase order it cites, summing the receipts against it, and
noticing that this invoice number has been seen before.

Keeping that split is the point. Vendor resolution is a query, and the moment a
query lives inside the matcher the matcher cannot be tested as a table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.domain.extraction import ExtractedInvoice
from threshold.domain.matching import (
    BankComparison,
    Disposition,
    InvoiceFacts,
    InvoiceLineFacts,
    MatchOutcome,
    PurchaseOrderFacts,
    PurchaseOrderLineFacts,
    normalise,
    three_way_match,
)
from threshold.domain.models import (
    Document,
    Extraction,
    GoodsReceiptLine,
    MatchException,
    MatchResult,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
    VendorBankAccount,
)
from threshold.domain.tolerance import DEFAULT_POLICY, ZERO, TolerancePolicy
from threshold.observability.metrics import record_match_decision, record_match_exceptions
from threshold.services.approvals import ApprovalService
from threshold.services.audit import Actor, record

log = structlog.get_logger(__name__)

#: Below this, a payment identifier was not read well enough to accuse a vendor
#: of changing their bank account.
#:
#: A module constant rather than a `TolerancePolicy` knob, and the difference is
#: not cosmetic. A tolerance answers "how much drift is acceptable"; this answers
#: "did we read it at all", which is identity rather than degree. It is also a
#: property of one parser's scoring tiers -- 0.88 same-line, 0.58 next-line, and
#: a 0.85x discount when two different values compete -- so 0.50 admits a
#: next-line read and refuses a next-line read that disagreed with itself
#: (0.493). Stamping that into the versioned policy would dress a second guess
#: as calibration, at the exact milestone D-014 says not to.
_BANK_READ_FLOOR: Final = Decimal("0.50")


class MatchingError(RuntimeError):
    """The match could not be attempted at all.

    Distinct from an invoice that fails to match: that is a normal outcome with
    exceptions attached. This means there was nothing to judge -- no document,
    no extraction -- and no tolerance setting changes it.
    """


class MatchingService:
    def __init__(self, policy: TolerancePolicy = DEFAULT_POLICY) -> None:
        self._policy = policy

    async def match_document(self, session: AsyncSession, document_id: uuid.UUID) -> MatchResult:
        """Judge a document's latest extraction and persist the result."""
        document = (
            await session.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if document is None:
            raise MatchingError(f"no such document: {document_id}")

        extraction = (
            await session.execute(
                select(Extraction)
                .where(Extraction.document_id == document_id)
                .order_by(Extraction.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if extraction is None:
            raise MatchingError(f"document {document_id} has no extraction to match")

        facts = await self._invoice_facts(session, document, extraction)
        order = await self._purchase_order_facts(session, facts.po_number)
        outcome = three_way_match(facts, order, policy=self._policy)

        return await self._persist(session, document, extraction, order, outcome)

    # ------------------------------------------------------------ reading ---

    async def _invoice_facts(
        self, session: AsyncSession, document: Document, extraction: Extraction
    ) -> InvoiceFacts:
        invoice = ExtractedInvoice.model_validate(extraction.fields)

        vendor_id, vendor_status = await self._resolve_vendor(session, invoice)
        duplicate_of = await self._find_prior_billing(session, document, invoice)

        lines = tuple(
            InvoiceLineFacts(
                line_number=index,
                description=item.description.value or "",
                quantity=item.quantity.value or ZERO,
                unit_price=item.unit_price.value or ZERO,
                line_total=item.line_total.value or ZERO,
                sku=item.sku.value if item.sku else None,
            )
            for index, item in enumerate(invoice.line_items, start=1)
        )

        return InvoiceFacts(
            invoice_number=invoice.invoice_number.value or "",
            currency=(invoice.currency.value or "").upper(),
            # A field that did not parse reads as zero rather than being dropped.
            # Zero is wrong in a way the arithmetic check catches loudly; a
            # dropped field is wrong in a way that looks like agreement.
            subtotal=invoice.subtotal.value or ZERO,
            tax_amount=invoice.tax_amount.value or ZERO,
            total=invoice.total.value or ZERO,
            lines=lines,
            lowest_field_confidence=_decision_confidence(invoice, resolved=vendor_id is not None),
            vendor_id=vendor_id,
            vendor_status=vendor_status,
            po_number=(invoice.po_number.value or None),
            duplicate_of_document_id=duplicate_of,
            bank_comparison=await self._compare_bank_details(session, invoice, vendor_id),
        )

    async def _compare_bank_details(
        self,
        session: AsyncSession,
        invoice: ExtractedInvoice,
        vendor_id: uuid.UUID | None,
    ) -> BankComparison:
        """Do the payment details on the page match the ones we hold?

        Tail-to-tail, on the last four digits of the account and of the routing
        code. Not the stored fingerprint: that is an HMAC over the *full*
        routing and account numbers, and the page carries four digits of
        nineteen by deliberate design. D-013's original plan named the
        fingerprint and was not executable; this corrects it.

        Both identifiers must read, or the answer is NOT_READ. A routing-only
        match is agreement about which *institution* is being paid while never
        having seen the destination account, which is the most dangerous
        available false negative -- and all four layouts print both, so
        insisting costs nothing.
        """
        if vendor_id is None:
            return BankComparison.NO_VENDOR

        account_field = invoice.bank_account_last4
        routing_field = invoice.bank_routing_last4
        if account_field is None or routing_field is None:
            return BankComparison.NOT_EXTRACTED
        if (
            account_field.value is None
            or routing_field.value is None
            or account_field.confidence < _BANK_READ_FLOOR
            or routing_field.confidence < _BANK_READ_FLOOR
        ):
            return BankComparison.NOT_READ

        # Every row, not just the open one: the retired/unknown distinction
        # needs the history anyway, and reading them all means a vendor with two
        # rows both claiming to be current -- a data-integrity bug -- fails
        # quiet rather than manufacturing a fraud alert.
        rows = (
            await session.execute(
                select(
                    VendorBankAccount.account_number_last4,
                    VendorBankAccount.routing_number_last4,
                    VendorBankAccount.effective_to,
                )
                .where(VendorBankAccount.vendor_id == vendor_id)
                .order_by(VendorBankAccount.effective_from.desc())
            )
        ).all()
        if not rows:
            return BankComparison.NO_HISTORY

        printed = (account_field.value, routing_field.value)
        if any(
            (r.account_number_last4, r.routing_number_last4) == printed
            for r in rows
            if r.effective_to is None
        ):
            return BankComparison.MATCHED
        if any((r.account_number_last4, r.routing_number_last4) == printed for r in rows):
            return BankComparison.CHANGED_TO_RETIRED
        return BankComparison.CHANGED

    async def _resolve_vendor(
        self, session: AsyncSession, invoice: ExtractedInvoice
    ) -> tuple[uuid.UUID | None, str | None]:
        """Tax identifier first, then an exact name.

        A tax identifier is issued by a government and is the closest thing to a
        primary key a vendor has. Names are typed by whoever raised the invoice
        and collide: "Acme Ltd", "Acme Limited" and "ACME LTD." are one supplier
        with three spellings, and a fuzzy matcher that unifies them will
        eventually unify two suppliers that are genuinely different. Failing to
        resolve raises an exception a human answers in seconds; resolving to the
        wrong vendor pays the wrong bank account.
        """
        tax_id = (invoice.vendor_tax_id.value or "").strip()
        if tax_id:
            found = (
                await session.execute(
                    select(Vendor.id, Vendor.status).where(Vendor.tax_id == tax_id)
                )
            ).one_or_none()
            if found is not None:
                return found.id, found.status

        # The status travels with the identity deliberately. Resolving the
        # vendor and then asking a second query whether it is on hold leaves a
        # window where the two disagree, and the direction that window fails in
        # is paying a held vendor.
        name = normalise(invoice.vendor_name.value or "")
        if not name:
            return None, None

        by_name = (
            await session.execute(
                select(Vendor.id, Vendor.status).where(
                    func.lower(Vendor.legal_name) == name,
                )
            )
        ).one_or_none()
        if by_name is None:
            by_name = (
                await session.execute(
                    select(Vendor.id, Vendor.status).where(
                        func.lower(Vendor.display_name) == name,
                    )
                )
            ).one_or_none()

        return (None, None) if by_name is None else (by_name.id, by_name.status)

    async def _find_prior_billing(
        self, session: AsyncSession, document: Document, invoice: ExtractedInvoice
    ) -> uuid.UUID | None:
        """Has this vendor already billed under this invoice number?

        A heuristic, which is why the code it raises is ``DUPLICATE_SUSPECTED``
        rather than ``DUPLICATE``. It compares the invoice number and the vendor
        name as they were read, across every other document's extractions.

        Note what this does *not* catch, because it matters: a genuine duplicate
        submitted as a rescanned image with a different byte hash and a
        differently-typed vendor name will slip past both this and the
        content-hash check in ingest. Payment-time idempotency in M8 is the
        control that actually closes that, and it closes it on the money rather
        than on the paperwork.
        """
        number = (invoice.invoice_number.value or "").strip()
        vendor_name = (invoice.vendor_name.value or "").strip()
        if not number or not vendor_name:
            return None

        stmt: Select[tuple[uuid.UUID]] = (
            select(Extraction.document_id)
            .where(
                Extraction.document_id != document.id,
                Extraction.fields["invoice_number"]["value"].astext == number,
                func.lower(Extraction.fields["vendor_name"]["value"].astext) == vendor_name.lower(),
            )
            .order_by(Extraction.created_at)
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _purchase_order_facts(
        self, session: AsyncSession, po_number: str | None
    ) -> PurchaseOrderFacts | None:
        if not po_number:
            return None

        order = (
            await session.execute(
                select(PurchaseOrder).where(PurchaseOrder.po_number == po_number.strip())
            )
        ).scalar_one_or_none()
        if order is None:
            return None

        # Receipts summed in the database rather than in Python. A purchase
        # order with a long delivery history would otherwise pull every receipt
        # line across the wire to add up four numbers.
        received = func.coalesce(func.sum(GoodsReceiptLine.quantity_received), 0)
        rows = (
            await session.execute(
                select(
                    PurchaseOrderLine.line_number,
                    PurchaseOrderLine.sku,
                    PurchaseOrderLine.description,
                    PurchaseOrderLine.quantity_ordered,
                    PurchaseOrderLine.unit_price,
                    received.label("quantity_received"),
                )
                .select_from(PurchaseOrderLine)
                .outerjoin(
                    GoodsReceiptLine,
                    GoodsReceiptLine.purchase_order_line_id == PurchaseOrderLine.id,
                )
                .where(PurchaseOrderLine.purchase_order_id == order.id)
                .group_by(PurchaseOrderLine.id)
                .order_by(PurchaseOrderLine.line_number)
            )
        ).all()

        return PurchaseOrderFacts(
            po_number=order.po_number,
            vendor_id=order.vendor_id,
            currency=order.currency,
            status=order.status,
            lines=tuple(
                PurchaseOrderLineFacts(
                    line_number=row.line_number,
                    description=row.description,
                    quantity_ordered=row.quantity_ordered,
                    unit_price=row.unit_price,
                    quantity_received=row.quantity_received,
                    sku=row.sku,
                )
                for row in rows
            ),
        )

    # ------------------------------------------------------------ writing ---

    async def _persist(
        self,
        session: AsyncSession,
        document: Document,
        extraction: Extraction,
        order: PurchaseOrderFacts | None,
        outcome: MatchOutcome,
    ) -> MatchResult:
        next_version = (
            await session.execute(
                select(func.coalesce(func.max(MatchResult.version), 0) + 1).where(
                    MatchResult.document_id == document.id
                )
            )
        ).scalar_one()

        purchase_order_id: uuid.UUID | None = None
        if order is not None:
            purchase_order_id = (
                await session.execute(
                    select(PurchaseOrder.id).where(PurchaseOrder.po_number == order.po_number)
                )
            ).scalar_one_or_none()

        result = MatchResult(
            document_id=document.id,
            extraction_id=extraction.id,
            purchase_order_id=purchase_order_id,
            version=next_version,
            disposition=outcome.disposition.value,
            policy_version=outcome.policy_version,
            line_pairs=[
                {"invoice_line": invoice_line, "po_line": po_line}
                for invoice_line, po_line in outcome.line_pairs
            ],
            skipped_checks=list(outcome.skipped_checks),
        )
        session.add(result)
        await session.flush()

        # One clock reading for the whole result. Stamping each exception with
        # its own now() would give two exceptions raised by the same decision
        # deadlines that differ by microseconds, which is noise in every report
        # that sorts by due date.
        raised_at = datetime.now(UTC)
        for exc in outcome.exceptions:
            session.add(
                MatchException(
                    match_result_id=result.id,
                    code=exc.code.value,
                    severity=exc.severity.value,
                    owner_role=exc.owner.value,
                    due_at=raised_at + timedelta(hours=exc.sla_hours),
                    detail=exc.detail,
                    invoice_line_number=exc.invoice_line_number,
                    purchase_order_line_number=exc.po_line_number,
                    breach=_breach_json(exc),
                )
            )

        await session.flush()

        # In the same transaction as the rows it describes. models.py has said
        # since M0 that every state transition writes one of these; this one
        # emitted a log line and nothing else, so the claim was false for the
        # single most consequential transition in the system.
        await record(
            session,
            actor=Actor.system(),
            action="match.decided",
            entity_type="match_result",
            entity_id=result.id,
            after={
                "document_id": str(document.id),
                "version": next_version,
                "disposition": outcome.disposition.value,
                "policy_version": outcome.policy_version,
                "exception_codes": sorted({e.code.value for e in outcome.exceptions}),
                "skipped_checks": len(outcome.skipped_checks),
            },
        )

        # The machine's own decision, written into the same table a person
        # writes to. Without this, "the system cleared it" is an absence of a
        # human row, which is indistinguishable from nobody having looked yet.
        if outcome.disposition is Disposition.AUTO_APPROVE:
            await ApprovalService().record_auto_approval(session, result)

        log.info(
            "match.decided",
            document_id=str(document.id),
            version=next_version,
            disposition=outcome.disposition.value,
            exceptions=len(outcome.exceptions),
            policy_version=outcome.policy_version,
        )

        # After the writes and outside the audit record, because a metric is
        # never allowed to be the reason a decision fails to persist. The
        # instruments drop their measurements when no reader is configured and
        # never raise on a dead collector; the one call that can raise is an
        # unhashable attribute value, which is why every label below is a
        # string off a frozen enum.
        #
        # `policy_version` is a label because the question this metric exists to
        # answer -- what share of invoices clear without a person -- is only
        # meaningful against the policy that was in force, and a tolerance
        # change is exactly the event that moves it.
        record_match_decision(
            disposition=outcome.disposition.value, policy_version=outcome.policy_version
        )
        record_match_exceptions(
            (exc.code.value, exc.severity.value, exc.owner.value) for exc in outcome.exceptions
        )
        return result


def _decision_confidence(invoice: ExtractedInvoice, *, resolved: bool) -> Decimal:
    """How well the decision's inputs were read, after corroboration.

    ``Extraction.lowest_decision_confidence`` is the honest score for the
    *reading*, and it is stored unchanged. This is a different question: how
    much the *match* can rely on those values.

    The two diverge on the vendor name. The deterministic tier scores it 0.45 by
    construction, because finding an unlabelled name near the top of a page is a
    genuinely weak heuristic. But once that string has matched a row in the
    vendor master, the identity is no longer resting on the heuristic -- an
    independent source agreed. A name that reads poorly and still resolves is
    corroborated, not uncertain, and this is the same argument the extractor
    already makes when subtotal plus tax reconciles to the total.

    A name that fails to resolve is not discounted here at all: it has already
    produced VENDOR_UNKNOWN, which blocks.
    """
    fields = dict(invoice.decision_relevant_fields())
    if resolved:
        fields.pop("vendor_name", None)
    return Decimal(str(round(min((f.confidence for f in fields.values()), default=0.0), 4)))


def _breach_json(exc: object) -> dict[str, object] | None:
    breach = getattr(exc, "breach", None)
    if breach is None:
        return None
    return {
        "expected": str(breach.expected),
        "actual": str(breach.actual),
        "variance": str(breach.variance),
        "variance_pct": None if breach.variance_pct is None else str(breach.variance_pct),
        "tripped_percentage": breach.tripped_percentage,
        "tripped_cap": breach.tripped_cap,
    }


__all__ = ["Disposition", "MatchingError", "MatchingService"]
