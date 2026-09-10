"""What an extractor produces, independent of which extractor produced it.

Two properties matter more than the field list.

**Confidence is per field, not per document.** A document-level score cannot
answer the only question that matters operationally — *may this specific value
be used to make a decision?* An invoice can have a perfectly legible total and
an illegible purchase-order number, and those two facts route differently: the
first is usable, the second must stop and ask a human. A single number averages
that distinction away.

**Every extraction is immutable and versioned.** Re-extracting a document
inserts a new row rather than mutating the old one, which is what makes the
evaluation harness possible: any historical document can be replayed against a
new model or prompt and the results diffed. Without it, "the model changed and
the numbers moved" is a mystery rather than a diff.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractorKind(StrEnum):
    """Which tier produced a result.

    The deterministic tier exists so the whole pipeline runs hermetically in CI
    with no model weights, no API keys and no network. It is genuinely worse and
    it is labelled worse — every stored extraction records which tier produced
    it, so an evaluation can never silently average the two together.
    """

    DETERMINISTIC = "deterministic"
    VISION_MODEL = "vision_model"


#: Confidence is a probability. Anything outside [0, 1] is a bug in an adapter,
#: not an unusual reading, so the type refuses it rather than clamping.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class BoundingBox(BaseModel):
    """Where on the page a value was read from, normalised to [0, 1].

    Normalised rather than absolute so a value stays locatable when the same
    document is re-rendered at a different resolution — and so the operator
    console can highlight the source region without knowing the page size.
    """

    model_config = ConfigDict(frozen=True)

    page: int = Field(ge=1)
    x0: Annotated[float, Field(ge=0.0, le=1.0)]
    y0: Annotated[float, Field(ge=0.0, le=1.0)]
    x1: Annotated[float, Field(ge=0.0, le=1.0)]
    y1: Annotated[float, Field(ge=0.0, le=1.0)]

    @model_validator(mode="after")
    def _corners_ordered(self) -> BoundingBox:
        """Reject an inverted box rather than silently yielding a negative area.

        An adapter that swaps corners produces a box that highlights nothing in
        the console and scores oddly in evaluation. Failing here makes that a
        loud bug in one adapter instead of a quiet one everywhere downstream.
        """
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError(
                f"bounding box corners are inverted or degenerate: "
                f"({self.x0}, {self.y0}) -> ({self.x1}, {self.y1})"
            )
        return self

    @property
    def area(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)


class ExtractedField[T](BaseModel):
    """One value, with how sure the extractor is and where it came from."""

    model_config = ConfigDict(frozen=True)

    value: T | None
    confidence: Confidence
    bbox: BoundingBox | None = None
    #: Verbatim text the value was parsed from. Kept because a mis-parse and a
    #: mis-read are different failures and the raw span is what distinguishes
    #: them when a reviewer disagrees with the extractor.
    raw_text: str | None = None

    @property
    def is_present(self) -> bool:
        return self.value is not None


class ExtractedLineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: ExtractedField[str]
    quantity: ExtractedField[Decimal]
    unit_price: ExtractedField[Decimal]
    line_total: ExtractedField[Decimal]
    sku: ExtractedField[str] | None = None

    @property
    def min_confidence(self) -> float:
        present = [
            f.confidence
            for f in (self.description, self.quantity, self.unit_price, self.line_total)
        ]
        return min(present) if present else 0.0


class ExtractedInvoice(BaseModel):
    """The schema an extractor is constrained to produce.

    Every field is optional at the type level and carries its own confidence,
    because "the extractor could not find a purchase-order number" is a normal,
    expected outcome that must round-trip through the system as data rather than
    as a parse failure. A third of real invoices have no PO number at all.
    """

    model_config = ConfigDict(frozen=True)

    document_type: ExtractedField[Literal["invoice", "credit_note", "unknown"]]

    invoice_number: ExtractedField[str]
    vendor_name: ExtractedField[str]
    vendor_tax_id: ExtractedField[str]
    po_number: ExtractedField[str]

    issue_date: ExtractedField[str]
    due_date: ExtractedField[str]

    currency: ExtractedField[str]
    subtotal: ExtractedField[Decimal]
    tax_amount: ExtractedField[Decimal]
    total: ExtractedField[Decimal]

    #: The masked payment details printed on the page: the last four digits of
    #: the account or IBAN, and of the routing code, sort code or BLZ.
    #:
    #: ``| None = None`` rather than a default ``ExtractedField`` instance, and
    #: the distinction carries weight on this field. ``Extraction.fields`` is a
    #: JSONB blob written once, so rows persisted before this existed have no
    #: such key. A default instance would make "this reading predates payment-
    #: detail extraction" byte-identical to "the extractor looked and found
    #: nothing" -- two different facts with two different remediations, one of
    #: which is re-extraction. On a fraud control, never-asked must not read as
    #: asked-and-clear.
    #:
    #: Deliberately absent from ``decision_relevant_fields``: that gate is a
    #: strict minimum, an unreadable bank line scores 0.0, and every invoice
    #: without a printed payment block would trip LOW_CONFIDENCE_FIELD. The
    #: bank signal has its own first-class path through the exception layer.
    bank_account_last4: ExtractedField[str] | None = None
    bank_routing_last4: ExtractedField[str] | None = None

    line_items: tuple[ExtractedLineItem, ...] = ()

    def decision_relevant_fields(self) -> dict[str, ExtractedField[object]]:
        """The fields a matching or payment decision actually depends on.

        Confidence on a vendor's postal address does not gate anything. These
        do, which is why the auto-approval gate reads this subset rather than a
        document-level average.
        """
        return {
            "invoice_number": self.invoice_number,  # type: ignore[dict-item]
            "vendor_name": self.vendor_name,  # type: ignore[dict-item]
            "total": self.total,  # type: ignore[dict-item]
            "currency": self.currency,  # type: ignore[dict-item]
            "issue_date": self.issue_date,  # type: ignore[dict-item]
        }

    def lowest_decision_confidence(self) -> float:
        """The weakest link among the fields that gate a decision.

        Deliberately a minimum rather than a mean. A mean lets four confident
        fields carry one unreadable total past the threshold, which is exactly
        the failure this system exists to prevent.
        """
        fields = self.decision_relevant_fields().values()
        return min((f.confidence for f in fields), default=0.0)


class ExtractionResult(BaseModel):
    """An extraction plus the provenance needed to reproduce or audit it."""

    model_config = ConfigDict(frozen=True)

    invoice: ExtractedInvoice
    extractor_kind: ExtractorKind
    #: Identifies the exact extractor. A model name and version, or the
    #: deterministic tier's own version so its output is diffable across changes.
    extractor_id: str
    prompt_version: str | None = None
    duration_ms: int = Field(ge=0)
    #: Populated by model-backed tiers; None for the deterministic tier, which
    #: has no token cost. Stored so cost per document is measurable rather than
    #: estimated.
    input_tokens: int | None = None
    output_tokens: int | None = None


#: Keys inside ``Extraction.fields`` that must never leave the internal
#: projection. The blob is one JSONB column, so it cannot carry a per-field
#: sensitivity the way a table column does -- this is the equivalent, declared
#: in one place and consumed by anything that renders the blob outward.
#:
#: The whole column is not marked RESTRICTED instead, because
#: ``classification.py``'s rule for that level is *dropped, not masked*: doing so
#: would delete the entire extraction from the review projection in order to
#: protect two keys, and a reviewer cannot judge an invoice they cannot see.
RESTRICTED_FIELD_KEYS: Final = frozenset(
    {
        "bank_account_last4",
        "bank_routing_last4",
        # The same datum as Vendor.tax_id, which is declared RESTRICTED on the
        # column. Read off the page it is no less identifying, and leaving it
        # out here classified one copy of a government identifier as secret
        # while serving the other to anyone who asked. The matcher still uses
        # it: this set governs the projection, not the stored blob.
        "vendor_tax_id",
    }
)


def redacted_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """The extraction blob with payment details removed.

    Dropped rather than masked, for the reason ``classification.py`` gives: a
    masked bank identifier is still a bank identifier to anyone holding a
    candidate list, and four digits is a short list.
    """
    return {key: value for key, value in fields.items() if key not in RESTRICTED_FIELD_KEYS}
