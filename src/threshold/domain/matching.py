"""Three-way match: what was ordered, what arrived, what is being billed.

Pure functions over plain values. Nothing here touches a database, a model or a
clock, which is what makes the decision layer testable as a table rather than as
a scenario -- and what makes "why was this blocked?" answerable by reading a
function instead of replaying a run.

**The disposition is derived, never decided.** There is no rule that says "block
this one". Severity lives on the exception in the taxonomy, and the outcome is
the worst severity present. A separate disposition rule would be a second source
of truth that drifts from the first, and the drift would show up as an invoice
that reports a blocking exception and pays anyway.

**Every applicable check runs.** Collecting the full set costs nothing and saves
the reviewer three round trips: an invoice with a bad price *and* an arithmetic
error should say so once, not reveal the second problem after the first is
fixed. The exception is checks that would be meaningless -- comparing a unit
price of 100 EUR against 100 USD produces a variance of zero, which is not a
match, it is a category error. Those are skipped explicitly and the reason is
recorded.

**Pairing invoice lines to purchase-order lines is conservative.** A SKU is
authoritative. A description is a heuristic, so only an exact match after
normalisation counts. Fuzzy pairing would raise the match rate and lower the
value of the result: an unmatched line is a question, whereas a wrongly matched
line is a confident wrong answer that compares two unrelated prices and reports
whatever falls out.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from threshold.domain.taxonomy import ExceptionCode, OwnerRole, Severity, spec_for
from threshold.domain.tolerance import (
    DEFAULT_POLICY,
    SCALE,
    ZERO,
    Breach,
    TolerancePolicy,
)

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Casefold and collapse whitespace.

    Deliberately not stemming, not stripping punctuation, not fuzzy. Everything
    beyond this raises the pairing rate by pairing things that are not the same.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


class Disposition(StrEnum):
    AUTO_APPROVE = "auto_approve"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class InvoiceLineFacts:
    line_number: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    sku: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceFacts:
    """The invoice, normalised and with vendor resolution already attempted.

    Resolution happens before this: deciding *which* vendor an invoice is from
    needs the database, and mixing that lookup into the matcher would make the
    matcher untestable without one.
    """

    invoice_number: str
    currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    lines: tuple[InvoiceLineFacts, ...]
    lowest_field_confidence: Decimal
    vendor_id: uuid.UUID | None = None
    po_number: str | None = None
    #: Set when this vendor has already billed under this invoice number.
    duplicate_of_document_id: uuid.UUID | None = None
    #: Set when the payment details differ from those last used for this vendor.
    bank_details_changed: bool = False


@dataclass(frozen=True, slots=True)
class PurchaseOrderLineFacts:
    """A purchase-order line with its receipts already aggregated.

    ``quantity_received`` is the sum across every goods receipt against this
    line, not one receipt. Partial deliveries are normal, and a matcher that
    compared against a single receipt would raise an exception on every
    shipment after the first.
    """

    line_number: int
    description: str
    quantity_ordered: Decimal
    unit_price: Decimal
    quantity_received: Decimal
    sku: str | None = None


@dataclass(frozen=True, slots=True)
class PurchaseOrderFacts:
    po_number: str
    vendor_id: uuid.UUID
    currency: str
    status: str
    lines: tuple[PurchaseOrderLineFacts, ...]


@dataclass(frozen=True, slots=True)
class MatchException:
    code: ExceptionCode
    severity: Severity
    owner: OwnerRole
    sla_hours: int
    detail: str
    invoice_line_number: int | None = None
    po_line_number: int | None = None
    breach: Breach | None = None


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    disposition: Disposition
    policy_version: str
    exceptions: tuple[MatchException, ...]
    #: Invoice line number -> purchase-order line number, for the pairs that
    #: resolved. Persisted so a reviewer can see what the matcher believed, and
    #: so a wrong pairing is visible rather than inferred from its consequences.
    line_pairs: tuple[tuple[int, int], ...]
    #: Checks that were skipped because running them would have been misleading.
    skipped_checks: tuple[str, ...]

    @property
    def is_blocked(self) -> bool:
        return self.disposition is Disposition.BLOCKED


def _raise(
    code: ExceptionCode,
    detail: str,
    *,
    invoice_line: int | None = None,
    po_line: int | None = None,
    breach: Breach | None = None,
) -> MatchException:
    spec = spec_for(code)
    return MatchException(
        code=code,
        severity=spec.severity,
        owner=spec.owner,
        sla_hours=spec.sla_hours,
        detail=detail,
        invoice_line_number=invoice_line,
        po_line_number=po_line,
        breach=breach,
    )


def _disposition_for(exceptions: tuple[MatchException, ...]) -> Disposition:
    """The worst severity present. See the module docstring."""
    severities = {e.severity for e in exceptions}
    if Severity.BLOCK in severities:
        return Disposition.BLOCKED
    if Severity.REVIEW in severities:
        return Disposition.NEEDS_REVIEW
    return Disposition.AUTO_APPROVE


def _pair_lines(
    invoice: InvoiceFacts, po: PurchaseOrderFacts
) -> tuple[dict[int, PurchaseOrderLineFacts], list[MatchException]]:
    """Resolve each invoice line to a purchase-order line.

    SKU first, then an exact normalised description. Anything unresolved is an
    exception rather than a guess.
    """
    by_sku = {normalise(line.sku): line for line in po.lines if line.sku}
    by_description = {normalise(line.description): line for line in po.lines}

    paired: dict[int, PurchaseOrderLineFacts] = {}
    problems: list[MatchException] = []

    for line in invoice.lines:
        match = by_sku.get(normalise(line.sku)) if line.sku else None
        if match is None:
            match = by_description.get(normalise(line.description))
        if match is None:
            problems.append(
                _raise(
                    ExceptionCode.LINE_NOT_ON_PO,
                    f"invoice line {line.line_number} ({line.description!r}) "
                    f"has no counterpart on purchase order {po.po_number}",
                    invoice_line=line.line_number,
                )
            )
            continue
        paired[line.line_number] = match

    return paired, problems


def _check_document_arithmetic(
    invoice: InvoiceFacts, policy: TolerancePolicy
) -> list[MatchException]:
    """Does the invoice agree with itself?

    Runs whether or not a purchase order was found. An invoice whose parts do
    not add up is wrong on its own terms, and finding that out does not require
    knowing what was ordered.
    """
    problems: list[MatchException] = []

    stated = (invoice.subtotal + invoice.tax_amount).quantize(SCALE)
    breach = policy.total.breach(actual=invoice.total, expected=stated)
    if breach is None:
        breach = policy.total.breach(actual=stated, expected=invoice.total)
    if breach is not None:
        problems.append(
            _raise(
                ExceptionCode.ARITHMETIC_MISMATCH,
                f"subtotal {invoice.subtotal} plus tax {invoice.tax_amount} "
                f"is {stated}, but the invoice states {invoice.total}",
                breach=breach,
            )
        )

    for line in invoice.lines:
        computed = (line.quantity * line.unit_price).quantize(SCALE)
        line_breach = policy.total.breach(actual=line.line_total, expected=computed)
        if line_breach is None:
            line_breach = policy.total.breach(actual=computed, expected=line.line_total)
        if line_breach is not None:
            problems.append(
                _raise(
                    ExceptionCode.LINE_TOTAL_MISMATCH,
                    f"line {line.line_number}: {line.quantity} x {line.unit_price} "
                    f"is {computed}, but the line states {line.line_total}",
                    invoice_line=line.line_number,
                    breach=line_breach,
                )
            )

    return problems


def _check_lines(
    invoice: InvoiceFacts,
    po: PurchaseOrderFacts,
    paired: dict[int, PurchaseOrderLineFacts],
    policy: TolerancePolicy,
) -> list[MatchException]:
    """Price per line, quantity per purchase-order line.

    Price is per invoice line because each line carries its own price. Quantity
    is aggregated across every invoice line that resolved to the same
    purchase-order line: billing one ordered line across two invoice lines is
    legitimate, and checking them separately would compare half a shipment
    against the whole receipt and report a shortfall that is not there.
    """
    problems: list[MatchException] = []
    invoice_by_number = {line.line_number: line for line in invoice.lines}

    for invoice_line_number, po_line in paired.items():
        line = invoice_by_number[invoice_line_number]
        breach = policy.price.breach(actual=line.unit_price, expected=po_line.unit_price)
        if breach is not None:
            problems.append(
                _raise(
                    ExceptionCode.PRICE_VARIANCE,
                    f"line {line.line_number}: billed {line.unit_price} against an agreed "
                    f"{po_line.unit_price} ({breach.variance} over)",
                    invoice_line=line.line_number,
                    po_line=po_line.line_number,
                    breach=breach,
                )
            )

    billed_per_po_line: dict[int, Decimal] = defaultdict(lambda: ZERO)
    po_lines_by_number = {line.line_number: line for line in po.lines}
    for invoice_line_number, po_line in paired.items():
        billed_per_po_line[po_line.line_number] += invoice_by_number[invoice_line_number].quantity

    for po_line_number, billed in sorted(billed_per_po_line.items()):
        po_line = po_lines_by_number[po_line_number]

        # Nothing received is a different problem from too much billed, even
        # though the arithmetic is identical. See the taxonomy on DQ vs DW.
        if po_line.quantity_received <= ZERO:
            problems.append(
                _raise(
                    ExceptionCode.RECEIPT_MISSING,
                    f"purchase-order line {po_line_number} has been billed for {billed} "
                    "but nothing has been recorded as received",
                    po_line=po_line_number,
                )
            )
            continue

        over = policy.quantity.breach(actual=billed, expected=po_line.quantity_received)
        if over is not None:
            problems.append(
                _raise(
                    ExceptionCode.QUANTITY_VARIANCE,
                    f"purchase-order line {po_line_number}: billed {billed} against "
                    f"{po_line.quantity_received} received ({over.variance} over)",
                    po_line=po_line_number,
                    breach=over,
                )
            )
            continue

        under = policy.quantity.breach(actual=po_line.quantity_received, expected=billed)
        if under is not None:
            problems.append(
                _raise(
                    ExceptionCode.UNDER_BILLED,
                    f"purchase-order line {po_line_number}: billed {billed} against "
                    f"{po_line.quantity_received} received ({under.variance} under)",
                    po_line=po_line_number,
                    breach=under,
                )
            )

    return problems


def three_way_match(
    invoice: InvoiceFacts,
    po: PurchaseOrderFacts | None,
    *,
    policy: TolerancePolicy = DEFAULT_POLICY,
) -> MatchOutcome:
    """Compare an invoice against its purchase order and the goods received."""
    problems: list[MatchException] = []
    skipped: list[str] = []
    pairs: list[tuple[int, int]] = []

    # --- things that are true regardless of whether a PO was found ---------

    if invoice.lowest_field_confidence < policy.minimum_field_confidence:
        problems.append(
            _raise(
                ExceptionCode.LOW_CONFIDENCE_FIELD,
                f"the least confident field the decision reads scored "
                f"{invoice.lowest_field_confidence}, below the {policy.minimum_field_confidence} "
                "required to decide without a human",
            )
        )

    if invoice.duplicate_of_document_id is not None:
        problems.append(
            _raise(
                ExceptionCode.DUPLICATE_SUSPECTED,
                f"invoice number {invoice.invoice_number} has already been billed by this "
                f"vendor on document {invoice.duplicate_of_document_id}",
            )
        )

    if invoice.bank_details_changed:
        problems.append(
            _raise(
                ExceptionCode.BANK_DETAILS_CHANGED,
                "the payment details on this invoice differ from those last used for this vendor",
            )
        )

    if invoice.vendor_id is None:
        problems.append(
            _raise(
                ExceptionCode.VENDOR_UNKNOWN,
                "the billing party could not be resolved to a known vendor",
            )
        )

    problems.extend(_check_document_arithmetic(invoice, policy))

    # --- can we match at all? ----------------------------------------------

    if invoice.po_number is None:
        problems.append(
            _raise(ExceptionCode.NO_PO_REFERENCE, "the invoice cites no purchase order")
        )
        skipped.append("line matching: the invoice cites no purchase order")
        return _outcome(problems, pairs, skipped, policy)

    if po is None:
        problems.append(
            _raise(
                ExceptionCode.PO_NOT_FOUND,
                f"purchase order {invoice.po_number} is not on file",
            )
        )
        skipped.append(f"line matching: purchase order {invoice.po_number} is not on file")
        return _outcome(problems, pairs, skipped, policy)

    if po.status != "open":
        problems.append(
            _raise(
                ExceptionCode.PO_CLOSED,
                f"purchase order {po.po_number} is {po.status} and should not be billed against",
            )
        )

    if invoice.vendor_id is not None and invoice.vendor_id != po.vendor_id:
        problems.append(
            _raise(
                ExceptionCode.VENDOR_MISMATCH,
                f"the invoice resolves to vendor {invoice.vendor_id} but purchase order "
                f"{po.po_number} was raised with {po.vendor_id}",
            )
        )

    # A price comparison across currencies is not a loose comparison, it is a
    # meaningless one: 100 EUR against 100 USD yields a variance of zero and
    # would report a clean match on an invoice nobody should pay. Refusing to
    # answer is the correct answer, and it is recorded so the gap is visible.
    if normalise(invoice.currency) != normalise(po.currency):
        problems.append(
            _raise(
                ExceptionCode.CURRENCY_MISMATCH,
                f"the invoice is in {invoice.currency} but purchase order {po.po_number} "
                f"is in {po.currency}",
            )
        )
        skipped.append(
            f"price and quantity matching: {invoice.currency} and {po.currency} are "
            "not comparable without a rate, and this system does not invent one"
        )
        return _outcome(problems, pairs, skipped, policy)

    paired, pairing_problems = _pair_lines(invoice, po)
    problems.extend(pairing_problems)
    pairs = sorted((inv, po_line.line_number) for inv, po_line in paired.items())
    problems.extend(_check_lines(invoice, po, paired, policy))

    return _outcome(problems, pairs, skipped, policy)


def _outcome(
    problems: list[MatchException],
    pairs: list[tuple[int, int]],
    skipped: list[str],
    policy: TolerancePolicy,
) -> MatchOutcome:
    ordered = tuple(
        sorted(
            problems,
            key=lambda e: (
                [Severity.BLOCK, Severity.REVIEW, Severity.INFO].index(e.severity),
                e.code.value,
                e.invoice_line_number or 0,
                e.po_line_number or 0,
            ),
        )
    )
    return MatchOutcome(
        disposition=_disposition_for(ordered),
        policy_version=policy.version,
        exceptions=ordered,
        line_pairs=tuple(pairs),
        skipped_checks=tuple(skipped),
    )
