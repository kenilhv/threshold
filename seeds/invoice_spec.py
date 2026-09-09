"""The contract every fixture layout renders from.

One spec, many layouts. This separation is what makes the golden set useful:
the same invoice can be rendered as a dense European tabular document, a clean
American services invoice, or a skewed thermal scan, and the extractor is
expected to produce the same structured result from all three. Any divergence
is a layout sensitivity worth measuring rather than a mystery.

Frozen, and money is Decimal throughout. A fixture generator that drifts is a
golden set that silently stops being golden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class LineItem:
    description: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal
    unit_of_measure: str = "EA"
    sku: str | None = None

    def __post_init__(self) -> None:
        for name in ("quantity", "unit_price", "line_total"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"LineItem.{name} must be Decimal, never float")


@dataclass(frozen=True, slots=True)
class InvoiceSpec:
    """Everything a renderer needs, and nothing about how it looks."""

    invoice_number: str

    vendor_name: str
    vendor_address_lines: tuple[str, ...]
    vendor_tax_id: str

    bill_to_lines: tuple[str, ...]

    issue_date: date
    due_date: date

    #: ISO-4217. Drives number formatting in layouts that localise.
    currency: str

    line_items: tuple[LineItem, ...]
    subtotal: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    total: Decimal

    payment_terms: str

    #: Last four only. The fixtures never carry a full account number, because a
    #: golden set is committed to version control and a realistic-looking full
    #: account number in a public repository is a liability with no upside.
    bank_iban_last4: str
    bank_routing_last4: str

    #: Absent on roughly a third of real invoices, which is why the matching
    #: stage cannot assume it.
    po_number: str | None = None

    notes: str | None = None

    #: Seeds every random choice a renderer makes. Same seed, same bytes.
    seed: int = 0

    #: Set by the fixture builder, not by renderers. The expected extraction
    #: result for this document — the labels the eval harness scores against.
    expected_fields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("subtotal", "tax_rate", "tax_amount", "total"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"InvoiceSpec.{name} must be Decimal, never float")
        if len(self.currency) != 3:
            raise ValueError("currency must be a 3-letter ISO-4217 code")
        if self.due_date < self.issue_date:
            raise ValueError("due_date precedes issue_date")

    @property
    def is_credit_note(self) -> bool:
        return self.total < 0
