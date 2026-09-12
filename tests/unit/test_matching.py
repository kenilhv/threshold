"""The decision layer, as a table.

The claim in ``docs/ARCHITECTURE.md`` is that classification and tolerance
arithmetic are deterministic and therefore checkable rather than estimable.
This file is where that claim is either kept or exposed: every case states the
inputs and the exact set of exception codes expected, so a change in behaviour
shows up as a diff in a table instead of a shift in a score.

No stack required. Nothing here touches a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest

from threshold.domain.matching import (
    BankComparison,
    Disposition,
    InvoiceFacts,
    InvoiceLineFacts,
    PurchaseOrderFacts,
    PurchaseOrderLineFacts,
    three_way_match,
)
from threshold.domain.taxonomy import ExceptionCode, OwnerRole, Severity, spec_for

VENDOR = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_VENDOR = uuid.UUID("22222222-2222-4222-8222-222222222222")
PRIOR_DOCUMENT = uuid.UUID("33333333-3333-4333-8333-333333333333")

D = Decimal


def inv_line(
    *,
    number: int = 1,
    description: str = "Widget, 40mm",
    quantity: str = "10",
    unit_price: str = "5.00",
    line_total: str | None = None,
    sku: str | None = "SKU-1",
) -> InvoiceLineFacts:
    return InvoiceLineFacts(
        line_number=number,
        description=description,
        quantity=D(quantity),
        unit_price=D(unit_price),
        line_total=D(line_total) if line_total is not None else D(quantity) * D(unit_price),
        sku=sku,
    )


def invoice(
    *,
    lines: tuple[InvoiceLineFacts, ...] | None = None,
    subtotal: str = "50.00",
    tax_amount: str = "5.00",
    total: str = "55.00",
    currency: str = "USD",
    confidence: str = "0.97",
    vendor_id: uuid.UUID | None = VENDOR,
    vendor_status: str | None = "active",
    po_number: str | None = "PO-1001",
    duplicate_of: uuid.UUID | None = None,
    bank: BankComparison = BankComparison.MATCHED,
) -> InvoiceFacts:
    return InvoiceFacts(
        invoice_number="INV-7",
        currency=currency,
        subtotal=D(subtotal),
        tax_amount=D(tax_amount),
        total=D(total),
        lines=lines if lines is not None else (inv_line(),),
        lowest_field_confidence=D(confidence),
        vendor_id=vendor_id,
        vendor_status=vendor_status,
        po_number=po_number,
        duplicate_of_document_id=duplicate_of,
        bank_comparison=bank,
    )


def po_line(
    *,
    number: int = 1,
    description: str = "Widget, 40mm",
    quantity_ordered: str = "10",
    unit_price: str = "5.00",
    quantity_received: str = "10",
    sku: str | None = "SKU-1",
) -> PurchaseOrderLineFacts:
    return PurchaseOrderLineFacts(
        line_number=number,
        description=description,
        quantity_ordered=D(quantity_ordered),
        unit_price=D(unit_price),
        quantity_received=D(quantity_received),
        sku=sku,
    )


def order(
    *,
    lines: tuple[PurchaseOrderLineFacts, ...] | None = None,
    currency: str = "USD",
    status: str = "open",
    vendor_id: uuid.UUID = VENDOR,
) -> PurchaseOrderFacts:
    return PurchaseOrderFacts(
        po_number="PO-1001",
        vendor_id=vendor_id,
        currency=currency,
        status=status,
        lines=lines if lines is not None else (po_line(),),
    )


@dataclass(frozen=True)
class Case:
    name: str
    invoice: InvoiceFacts
    po: PurchaseOrderFacts | None
    codes: set[ExceptionCode]
    disposition: Disposition


CASES: tuple[Case, ...] = (
    Case(
        name="everything agrees",
        invoice=invoice(),
        po=order(),
        codes=set(),
        disposition=Disposition.AUTO_APPROVE,
    ),
    # --- price ------------------------------------------------------------
    Case(
        name="unit price above the agreed price",
        invoice=invoice(lines=(inv_line(unit_price="6.00"),), subtotal="60.00", total="65.00"),
        po=order(),
        codes={ExceptionCode.PRICE_VARIANCE},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="a rounding difference in the price is absorbed, not escalated",
        invoice=invoice(
            lines=(inv_line(unit_price="5.002", line_total="50.02"),),
            subtotal="50.02",
            total="55.02",
        ),
        po=order(),
        codes=set(),
        disposition=Disposition.AUTO_APPROVE,
    ),
    Case(
        # 1% of a large unit price is under the percentage threshold and still
        # a hundred dollars. The cap is the instrument that catches it.
        name="a small percentage of a large price still trips the absolute cap",
        invoice=invoice(
            lines=(inv_line(quantity="1", unit_price="10100.00"),),
            subtotal="10100.00",
            tax_amount="0.00",
            total="10100.00",
        ),
        po=order(
            lines=(po_line(quantity_ordered="1", unit_price="10000.00", quantity_received="1"),)
        ),
        codes={ExceptionCode.PRICE_VARIANCE},
        disposition=Disposition.BLOCKED,
    ),
    # --- quantity ---------------------------------------------------------
    Case(
        name="billed for more than was received",
        invoice=invoice(
            lines=(inv_line(quantity="20"),), subtotal="100.00", tax_amount="10.00", total="110.00"
        ),
        po=order(lines=(po_line(quantity_ordered="20", quantity_received="10"),)),
        codes={ExceptionCode.QUANTITY_VARIANCE},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        # The DQ / DW distinction. Same arithmetic as over-billing, different
        # problem, different owner, and it does not block.
        name="billed before anything was received",
        invoice=invoice(),
        po=order(lines=(po_line(quantity_received="0"),)),
        codes={ExceptionCode.RECEIPT_MISSING},
        disposition=Disposition.NEEDS_REVIEW,
    ),
    Case(
        name="billed for less than was received is informational and never blocks",
        invoice=invoice(
            lines=(inv_line(quantity="5"),), subtotal="25.00", tax_amount="2.50", total="27.50"
        ),
        po=order(),
        codes={ExceptionCode.UNDER_BILLED},
        disposition=Disposition.AUTO_APPROVE,
    ),
    Case(
        name="a fractional quantity difference is below the floor",
        invoice=invoice(
            lines=(inv_line(quantity="10.5"),),
            subtotal="52.50",
            tax_amount="5.25",
            total="57.75",
        ),
        po=order(),
        codes=set(),
        disposition=Disposition.AUTO_APPROVE,
    ),
    Case(
        # Billing one ordered line across two invoice lines is legitimate.
        # Checked separately each would look like a half-shipment shortfall.
        name="two invoice lines against one purchase-order line are summed",
        invoice=invoice(
            lines=(
                inv_line(number=1, quantity="5"),
                inv_line(number=2, quantity="5"),
            )
        ),
        po=order(),
        codes=set(),
        disposition=Disposition.AUTO_APPROVE,
    ),
    # --- pairing ----------------------------------------------------------
    Case(
        name="an invoice line with no counterpart on the order",
        invoice=invoice(lines=(inv_line(sku="SKU-9", description="Consulting, unspecified"),)),
        po=order(),
        codes={ExceptionCode.LINE_NOT_ON_PO},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="lines pair on an exact description when no SKU is present",
        invoice=invoice(lines=(inv_line(sku=None, description="  WIDGET,   40mm "),)),
        po=order(lines=(po_line(sku=None),)),
        codes=set(),
        disposition=Disposition.AUTO_APPROVE,
    ),
    # --- structural -------------------------------------------------------
    Case(
        name="no purchase order is cited",
        invoice=invoice(po_number=None),
        po=None,
        codes={ExceptionCode.NO_PO_REFERENCE},
        disposition=Disposition.NEEDS_REVIEW,
    ),
    Case(
        name="the cited purchase order is not on file",
        invoice=invoice(),
        po=None,
        codes={ExceptionCode.PO_NOT_FOUND},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="the purchase order is closed",
        invoice=invoice(),
        po=order(status="closed"),
        codes={ExceptionCode.PO_CLOSED},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="the vendor could not be resolved",
        invoice=invoice(vendor_id=None),
        po=order(),
        codes={ExceptionCode.VENDOR_UNKNOWN},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        # The seeded Pinewood vendor exists for this: every invoice from a
        # held vendor stops however clean it looks.
        name="the vendor is on hold",
        invoice=invoice(vendor_status="on_hold"),
        po=order(),
        codes={ExceptionCode.VENDOR_ON_HOLD},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="an unresolved vendor does not also report a hold",
        invoice=invoice(vendor_id=None, vendor_status=None),
        po=order(),
        codes={ExceptionCode.VENDOR_UNKNOWN},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="the invoice is from a different vendor than the order",
        invoice=invoice(vendor_id=OTHER_VENDOR),
        po=order(),
        codes={ExceptionCode.VENDOR_MISMATCH},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="a duplicate invoice number for this vendor",
        invoice=invoice(duplicate_of=PRIOR_DOCUMENT),
        po=order(),
        codes={ExceptionCode.DUPLICATE_SUSPECTED},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="the payment details changed",
        invoice=invoice(bank=BankComparison.CHANGED),
        po=order(),
        codes={ExceptionCode.BANK_DETAILS_CHANGED},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="the invoice and the order are in different currencies",
        invoice=invoice(
            currency="EUR",
            lines=(inv_line(unit_price="500.00"),),
            subtotal="5000.00",
            tax_amount="0.00",
            total="5000.00",
        ),
        po=order(currency="USD"),
        codes={ExceptionCode.CURRENCY_MISMATCH},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        # Same code, different first phone call: an account the vendor once
        # used is a stale or replayed document, not a redirect attempt.
        name="the payment details are an account the vendor has replaced",
        invoice=invoice(bank=BankComparison.CHANGED_TO_RETIRED),
        po=order(),
        codes={ExceptionCode.BANK_DETAILS_CHANGED},
        disposition=Disposition.BLOCKED,
    ),
    # --- the document against itself --------------------------------------
    Case(
        name="subtotal plus tax does not reach the stated total",
        invoice=invoice(total="60.00"),
        po=order(),
        codes={ExceptionCode.ARITHMETIC_MISMATCH},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="the stated total is below subtotal plus tax",
        invoice=invoice(total="50.00"),
        po=order(),
        codes={ExceptionCode.ARITHMETIC_MISMATCH},
        disposition=Disposition.BLOCKED,
    ),
    Case(
        name="quantity times price does not equal the stated line total",
        invoice=invoice(lines=(inv_line(line_total="99.00"),)),
        po=order(),
        codes={ExceptionCode.LINE_TOTAL_MISMATCH},
        disposition=Disposition.NEEDS_REVIEW,
    ),
    # --- confidence -------------------------------------------------------
    Case(
        name="a field the decision depends on was read too poorly to use",
        invoice=invoice(confidence="0.42"),
        po=order(),
        codes={ExceptionCode.LOW_CONFIDENCE_FIELD},
        disposition=Disposition.NEEDS_REVIEW,
    ),
    # --- more than one thing wrong at once --------------------------------
    Case(
        name="several problems are all reported, not just the first",
        invoice=invoice(
            lines=(inv_line(unit_price="6.00"),),
            subtotal="60.00",
            total="70.00",
            confidence="0.10",
        ),
        po=order(),
        codes={
            ExceptionCode.PRICE_VARIANCE,
            ExceptionCode.ARITHMETIC_MISMATCH,
            ExceptionCode.LOW_CONFIDENCE_FIELD,
        },
        disposition=Disposition.BLOCKED,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_match(case: Case) -> None:
    outcome = three_way_match(case.invoice, case.po)
    assert {e.code for e in outcome.exceptions} == case.codes
    assert outcome.disposition == case.disposition


def test_every_exception_code_is_reachable() -> None:
    """No code in the taxonomy is aspirational.

    A taxonomy is a promise about what the system detects. An entry nothing can
    raise is a promise it does not keep, and the gap is invisible precisely
    because the code exists and reads as covered.
    """
    raised: set[ExceptionCode] = set()
    for case in CASES:
        raised |= {e.code for e in three_way_match(case.invoice, case.po).exceptions}
    assert raised == set(ExceptionCode)


def test_every_exception_carries_its_routing() -> None:
    """Category, owner and clock, on every exception the matcher emits."""
    for case in CASES:
        for exc in three_way_match(case.invoice, case.po).exceptions:
            spec = spec_for(exc.code)
            assert exc.owner == spec.owner
            assert exc.severity == spec.severity
            assert exc.sla_hours == spec.sla_hours > 0
            assert exc.detail.strip()


def test_a_currency_mismatch_refuses_to_compare_prices() -> None:
    """Comparing 100 EUR against 100 USD yields zero variance and a wrong answer.

    Declining to answer is the correct behaviour, and the decline is recorded
    rather than left as an absence a reader has to notice.
    """
    outcome = three_way_match(
        invoice(
            currency="EUR",
            lines=(inv_line(unit_price="500.00"),),
            subtotal="5000.00",
            tax_amount="0.00",
            total="5000.00",
        ),
        order(currency="USD"),
    )
    assert {e.code for e in outcome.exceptions} == {ExceptionCode.CURRENCY_MISMATCH}
    assert outcome.skipped_checks, "the skipped comparison must be recorded"
    assert "not comparable" in outcome.skipped_checks[0]


def test_the_disposition_is_the_worst_severity_present() -> None:
    outcome = three_way_match(
        invoice(confidence="0.10", bank=BankComparison.CHANGED),
        order(),
    )
    severities = {e.severity for e in outcome.exceptions}
    assert severities == {Severity.REVIEW, Severity.BLOCK}
    assert outcome.disposition is Disposition.BLOCKED
    assert outcome.is_blocked


def test_the_policy_version_is_stamped_on_the_outcome() -> None:
    """A decision that cannot name the thresholds it used cannot be reproduced."""
    outcome = three_way_match(invoice(), order())
    assert outcome.policy_version == "2026.09.2"


def test_line_pairs_are_recorded() -> None:
    outcome = three_way_match(
        invoice(lines=(inv_line(number=1), inv_line(number=2, sku="SKU-2", description="Bracket"))),
        order(lines=(po_line(number=1), po_line(number=2, sku="SKU-2", description="Bracket"))),
    )
    assert outcome.line_pairs == ((1, 1), (2, 2))


def test_an_invoice_with_no_line_items_records_that_nothing_was_compared() -> None:
    """The dangerous silence.

    Before line-item extraction existed the matcher received zero lines and
    reported a clean match, because there was nothing to disagree with. An
    invoice that was never compared must not read the same as one that was
    compared and passed.
    """
    outcome = three_way_match(invoice(lines=()), order())
    assert not outcome.exceptions
    assert outcome.skipped_checks
    assert "no line items" in outcome.skipped_checks[0]


class TestPaymentDetailComparison:
    """Every state the comparison can land in is either an exception or a skip.

    Silence is the one outcome that must never happen: a payment-detail check
    that did not run has to be visible as not having run, or a reviewer reads a
    clean result as evidence the account was verified.
    """

    def test_a_matched_account_raises_nothing_and_skips_nothing(self) -> None:
        outcome = three_way_match(invoice(bank=BankComparison.MATCHED), order())
        assert not outcome.exceptions
        assert not outcome.skipped_checks

    @pytest.mark.parametrize(
        "state",
        [BankComparison.CHANGED, BankComparison.CHANGED_TO_RETIRED],
    )
    def test_a_changed_account_blocks_and_goes_to_treasury(self, state: BankComparison) -> None:
        outcome = three_way_match(invoice(bank=state), order())
        raised = [e for e in outcome.exceptions if e.code is ExceptionCode.BANK_DETAILS_CHANGED]
        assert len(raised) == 1
        assert raised[0].owner is OwnerRole.TREASURY
        assert raised[0].severity is Severity.BLOCK
        assert outcome.disposition is Disposition.BLOCKED

    @pytest.mark.parametrize(
        "state",
        [
            BankComparison.NOT_EXTRACTED,
            BankComparison.NOT_READ,
            BankComparison.NO_VENDOR,
            BankComparison.NO_HISTORY,
        ],
    )
    def test_a_comparison_that_could_not_run_is_recorded(self, state: BankComparison) -> None:
        outcome = three_way_match(invoice(bank=state), order())
        assert not [
            e for e in outcome.exceptions if e.code is ExceptionCode.BANK_DETAILS_CHANGED
        ], "an unmade comparison must not accuse anyone"
        assert any(s.startswith("payment-detail comparison:") for s in outcome.skipped_checks), (
            f"{state} produced neither an exception nor a recorded skip"
        )

    def test_every_state_is_accounted_for(self) -> None:
        """Exactly one of three outcomes, for every state, forever.

        MATCHED is the only state allowed to be silent, because it is the only
        one where the comparison ran and agreed. Every other state either
        accuses or admits. A state added later without a branch surfaces here
        as silence rather than as a control that quietly stopped running.
        """
        for state in BankComparison:
            outcome = three_way_match(invoice(bank=state), order())
            if state is BankComparison.MATCHED:
                assert not outcome.exceptions and not outcome.skipped_checks
                continue
            raised = any(e.code is ExceptionCode.BANK_DETAILS_CHANGED for e in outcome.exceptions)
            skipped = any(
                s.startswith("payment-detail comparison:") for s in outcome.skipped_checks
            )
            assert raised != skipped, f"{state} produced neither an exception nor a skip"

    def test_the_detail_never_carries_the_digits(self) -> None:
        """The message is persisted and shown to reviewers; the digits are RESTRICTED."""
        for state in (BankComparison.CHANGED, BankComparison.CHANGED_TO_RETIRED):
            outcome = three_way_match(invoice(bank=state), order())
            for exc in outcome.exceptions:
                assert not any(ch.isdigit() for ch in exc.detail), exc.detail

    def test_an_unresolved_vendor_still_records_the_bank_skip(self) -> None:
        """skipped_checks has to stand alone.

        VENDOR_UNKNOWN explains it to someone reading the exceptions. Someone
        reading the skip list should not have to cross-reference to find out
        why the payment details were never compared.
        """
        outcome = three_way_match(
            invoice(vendor_id=None, vendor_status=None, bank=BankComparison.NO_VENDOR), order()
        )
        assert any("did not resolve" in s for s in outcome.skipped_checks)

    def test_the_comparison_runs_even_with_no_purchase_order(self) -> None:
        """The early return for a missing order must not skip a fraud control.

        An invoice citing no purchase order is among the likeliest shapes for a
        redirected payment to arrive in.
        """
        outcome = three_way_match(invoice(po_number=None, bank=BankComparison.CHANGED), None)
        codes = {e.code for e in outcome.exceptions}
        assert ExceptionCode.BANK_DETAILS_CHANGED in codes
        assert ExceptionCode.NO_PO_REFERENCE in codes
