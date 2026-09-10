"""The exception taxonomy, and who owns each kind.

*Frozen* means something specific here. A code is never removed and never
repurposed -- only added. Dashboards, routing rules, SLA reports and eventually
the labelled evaluation set all key off these strings, so quietly changing what
``PRICE_VARIANCE`` means reinterprets every historical decision that carries it.
Retiring a code is allowed; reusing one is not.

The distinctions are borrowed from SAP's Logistics Invoice Verification
tolerance keys, which are worth taking seriously not because SAP is elegant but
because those particular cuts survived several decades of real accounts-payable
practice. Three are worth calling out, because a taxonomy invented from first
principles usually misses them:

* **Billed-before-receipt is not over-billing.** SAP separates DQ (quantity
  variance) from DW (quantity variance where the received quantity is zero).
  They look identical in arithmetic -- billed exceeds received -- and they are
  different problems with different owners. Over-billing is a dispute with the
  vendor and belongs to the buyer. Nothing received yet is usually a paperwork
  lag and belongs to receiving. Collapsing them sends half of everything to the
  wrong desk.

* **Immaterial differences should be absorbed, not escalated.** SAP forms small
  differences automatically (BD). A rule that escalates an eleven-cent rounding
  discrepancy costs more in human attention than the money at stake, and it
  trains reviewers to approve without reading, which is how the expensive
  exception gets waved through too.

* **Under-billing is not an exception.** Tolerances are asymmetric on purpose:
  being charged less than agreed is not a control failure. It is recorded as
  informational so the variance stays visible, and it never blocks a payment.

Each entry carries a category, an owner and a clock, which is the minimum an
exception needs in order to be actionable rather than merely logged.

Every code here is raised by ``domain/matching.py`` and every owner here
receives work, which ``test_taxonomy.py`` asserts. A code nothing can raise, or
a role nothing routes to, is a claim about coverage that the system does not
keep -- and this taxonomy is the wrong place to keep one, because it is the
thing a reviewer reads to learn what the system can detect. Tax variance is the
obvious absence: checking it needs per-jurisdiction rates, this system has none,
and a rate invented to fill the gap would produce confident wrong answers about
somebody's VAT. It lands when the rates do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class OwnerRole(StrEnum):
    """Who resolves it.

    Routing follows where the authority to fix the problem actually sits, not
    where the document happens to be. Accounts payable cannot decide whether a
    price increase was agreed; a buyer can. A buyer cannot confirm a pallet
    arrived; receiving can.
    """

    AP_CLERK = "ap_clerk"
    BUYER = "buyer"
    RECEIVING = "receiving"
    TREASURY = "treasury"


class Severity(StrEnum):
    """What the exception does to the payment.

    ``BLOCK`` is reserved for cases where paying would be wrong, not merely
    unverified. A taxonomy where everything blocks is a taxonomy nobody reads.
    """

    BLOCK = "block"
    REVIEW = "review"
    INFO = "info"


class ExceptionCode(StrEnum):
    """Frozen. Add, never repurpose."""

    # --- the invoice cannot be tied to an agreement ------------------------
    NO_PO_REFERENCE = "no_po_reference"
    PO_NOT_FOUND = "po_not_found"
    PO_CLOSED = "po_closed"
    VENDOR_UNKNOWN = "vendor_unknown"
    VENDOR_ON_HOLD = "vendor_on_hold"
    VENDOR_MISMATCH = "vendor_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    DUPLICATE_SUSPECTED = "duplicate_suspected"

    # --- three-way match, line level ---------------------------------------
    PRICE_VARIANCE = "price_variance"
    QUANTITY_VARIANCE = "quantity_variance"
    RECEIPT_MISSING = "receipt_missing"
    LINE_NOT_ON_PO = "line_not_on_po"
    UNDER_BILLED = "under_billed"

    # --- the document does not agree with itself ---------------------------
    ARITHMETIC_MISMATCH = "arithmetic_mismatch"
    LINE_TOTAL_MISMATCH = "line_total_mismatch"

    # --- we are not confident enough to act --------------------------------
    LOW_CONFIDENCE_FIELD = "low_confidence_field"

    # --- fraud shapes ------------------------------------------------------
    BANK_DETAILS_CHANGED = "bank_details_changed"


@dataclass(frozen=True, slots=True)
class ExceptionSpec:
    """A category, an owner and a clock."""

    code: ExceptionCode
    severity: Severity
    owner: OwnerRole
    #: Hours to resolve. Not decoration: an exception with no deadline is a
    #: queue that grows, and the aged-exception count is the number that tells
    #: an AP manager whether this system is helping or just relocating work.
    sla_hours: int
    summary: str
    #: The SAP tolerance key this distinction descends from, where there is one.
    #: Provenance only -- no logic reads it.
    sap_lineage: str | None = None


TAXONOMY: Final[dict[ExceptionCode, ExceptionSpec]] = {
    ExceptionCode.NO_PO_REFERENCE: ExceptionSpec(
        code=ExceptionCode.NO_PO_REFERENCE,
        severity=Severity.REVIEW,
        owner=OwnerRole.AP_CLERK,
        sla_hours=48,
        summary="The invoice cites no purchase order, so there is nothing to match it against.",
        sap_lineage="AN",
    ),
    ExceptionCode.PO_NOT_FOUND: ExceptionSpec(
        code=ExceptionCode.PO_NOT_FOUND,
        severity=Severity.BLOCK,
        owner=OwnerRole.AP_CLERK,
        sla_hours=24,
        summary="The invoice cites a purchase order number that does not exist here.",
    ),
    ExceptionCode.PO_CLOSED: ExceptionSpec(
        code=ExceptionCode.PO_CLOSED,
        severity=Severity.BLOCK,
        owner=OwnerRole.BUYER,
        sla_hours=24,
        summary="The purchase order is closed or cancelled and should not receive further billing.",
    ),
    ExceptionCode.VENDOR_UNKNOWN: ExceptionSpec(
        code=ExceptionCode.VENDOR_UNKNOWN,
        severity=Severity.BLOCK,
        owner=OwnerRole.AP_CLERK,
        sla_hours=24,
        summary="The billing party could not be resolved to a known vendor.",
    ),
    ExceptionCode.VENDOR_ON_HOLD: ExceptionSpec(
        code=ExceptionCode.VENDOR_ON_HOLD,
        severity=Severity.BLOCK,
        owner=OwnerRole.AP_CLERK,
        # A week, not a day. A hold is a deliberate business state rather than
        # an error, so the clock measures how long nobody revisited the
        # decision -- not how long somebody has to fix a mistake.
        sla_hours=168,
        summary="The vendor is on hold, so no invoice from them may be paid however clean it is.",
    ),
    ExceptionCode.VENDOR_MISMATCH: ExceptionSpec(
        code=ExceptionCode.VENDOR_MISMATCH,
        severity=Severity.BLOCK,
        owner=OwnerRole.BUYER,
        sla_hours=8,
        summary="The invoice is from a different vendor than the purchase order was raised with.",
    ),
    ExceptionCode.CURRENCY_MISMATCH: ExceptionSpec(
        code=ExceptionCode.CURRENCY_MISMATCH,
        severity=Severity.BLOCK,
        owner=OwnerRole.BUYER,
        sla_hours=24,
        summary="The invoice is denominated in a different currency than the purchase order.",
    ),
    ExceptionCode.DUPLICATE_SUSPECTED: ExceptionSpec(
        code=ExceptionCode.DUPLICATE_SUSPECTED,
        severity=Severity.BLOCK,
        owner=OwnerRole.AP_CLERK,
        sla_hours=24,
        summary="This vendor has already billed under this invoice number.",
    ),
    ExceptionCode.PRICE_VARIANCE: ExceptionSpec(
        code=ExceptionCode.PRICE_VARIANCE,
        severity=Severity.BLOCK,
        owner=OwnerRole.BUYER,
        sla_hours=48,
        summary="A unit price is above the agreed purchase-order price by more than tolerance.",
        sap_lineage="PP",
    ),
    ExceptionCode.QUANTITY_VARIANCE: ExceptionSpec(
        code=ExceptionCode.QUANTITY_VARIANCE,
        severity=Severity.BLOCK,
        owner=OwnerRole.BUYER,
        sla_hours=48,
        summary="More was billed than was received, beyond tolerance.",
        sap_lineage="DQ",
    ),
    ExceptionCode.RECEIPT_MISSING: ExceptionSpec(
        code=ExceptionCode.RECEIPT_MISSING,
        severity=Severity.REVIEW,
        owner=OwnerRole.RECEIVING,
        sla_hours=72,
        summary="Billed before anything was recorded as received against this line.",
        sap_lineage="DW",
    ),
    ExceptionCode.LINE_NOT_ON_PO: ExceptionSpec(
        code=ExceptionCode.LINE_NOT_ON_PO,
        severity=Severity.BLOCK,
        owner=OwnerRole.BUYER,
        sla_hours=48,
        summary="An invoice line has no counterpart on the purchase order.",
    ),
    ExceptionCode.UNDER_BILLED: ExceptionSpec(
        code=ExceptionCode.UNDER_BILLED,
        severity=Severity.INFO,
        owner=OwnerRole.AP_CLERK,
        sla_hours=168,
        summary="Less was billed than was received. Recorded, never blocking.",
    ),
    ExceptionCode.ARITHMETIC_MISMATCH: ExceptionSpec(
        code=ExceptionCode.ARITHMETIC_MISMATCH,
        severity=Severity.BLOCK,
        owner=OwnerRole.AP_CLERK,
        sla_hours=24,
        summary="Subtotal plus tax does not equal the stated total.",
        sap_lineage="BD",
    ),
    ExceptionCode.LINE_TOTAL_MISMATCH: ExceptionSpec(
        code=ExceptionCode.LINE_TOTAL_MISMATCH,
        severity=Severity.REVIEW,
        owner=OwnerRole.AP_CLERK,
        sla_hours=24,
        summary="Quantity times unit price does not equal the stated line total.",
    ),
    ExceptionCode.LOW_CONFIDENCE_FIELD: ExceptionSpec(
        code=ExceptionCode.LOW_CONFIDENCE_FIELD,
        severity=Severity.REVIEW,
        owner=OwnerRole.AP_CLERK,
        sla_hours=24,
        summary="A field the decision depends on was not read confidently enough to rely on.",
    ),
    ExceptionCode.BANK_DETAILS_CHANGED: ExceptionSpec(
        code=ExceptionCode.BANK_DETAILS_CHANGED,
        severity=Severity.BLOCK,
        owner=OwnerRole.TREASURY,
        sla_hours=4,
        summary="Payment details differ from those last used for this vendor.",
    ),
}


def spec_for(code: ExceptionCode) -> ExceptionSpec:
    """Look up a code's routing.

    Total by construction: ``test_taxonomy.py`` asserts every member of the enum
    has an entry, so a new code cannot reach production unrouted. That is the
    specific failure this guards against -- an exception that is detected,
    recorded, and then sits in a queue nobody owns.
    """
    return TAXONOMY[code]
