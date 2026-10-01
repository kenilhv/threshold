"""Tolerances: how much disagreement is still a match.

A tolerance is the whole control. Set it too tight and every rounding difference
becomes a human decision, the queue backs up, and reviewers start approving
without reading -- which is worse than having no control, because now the
expensive exception gets waved through with the cheap ones. Set it too loose and
the system pays whatever it is billed.

**Why three numbers rather than one.** A single percentage fails on small items:
fifty percent of a ten-cent part is five cents, and nobody should be paged for
it. A single absolute amount fails on large ones: fifty dollars against a
two-million-dollar order is noise, and against a sixty-dollar order it is most
of the invoice. So each tolerance carries:

* ``absorb_below`` -- an absolute floor. Anything smaller is immaterial and is
  never an exception, whatever the percentage says. This is SAP's "form small
  differences automatically" (BD), and it is the number that decides whether
  reviewers trust the queue.
* ``percentage`` -- the relative threshold, which is the right instrument in the
  middle of the range where most real variance lives.
* ``absolute_cap`` -- an absolute ceiling. Above this it is an exception however
  small the percentage, so a tenth of a percent of a very large invoice cannot
  be absorbed in silence.

A variance breaches when it clears the floor **and** trips either the percentage
or the ceiling.

**Asymmetry is deliberate.** These thresholds are applied to over-billing only.
Being charged less than agreed is not a control failure, and treating it as one
spends the reviewer's attention defending the vendor's margin. Under-billing is
recorded as informational and never blocks.

**The policy is versioned and stamped on every decision.** Six months later,
"why was this approved?" has to be answerable, and the answer is only meaningful
if the thresholds in force at the time can be recovered. A policy that mutates
in place makes every historical decision unreproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

#: Money and quantities are `NUMERIC(18,4)` in the database, so comparisons
#: quantize to the same scale. Comparing an unrounded quotient against a stored
#: value is how a system decides a row disagrees with itself.
SCALE: Final = Decimal("0.0001")

ZERO: Final = Decimal("0")
HUNDRED: Final = Decimal("100")


@dataclass(frozen=True, slots=True)
class Tolerance:
    """One threshold triple. See the module docstring for why there are three."""

    absorb_below: Decimal
    percentage: Decimal
    absolute_cap: Decimal

    def __post_init__(self) -> None:
        if self.absorb_below < ZERO or self.absolute_cap < ZERO or self.percentage < ZERO:
            raise ValueError("tolerances are magnitudes and cannot be negative")
        if self.absorb_below > self.absolute_cap:
            # Otherwise the floor swallows the ceiling and the cap can never fire,
            # which reads as a configured control that silently does nothing.
            raise ValueError("absorb_below must not exceed absolute_cap")

    def breach(self, *, actual: Decimal, expected: Decimal) -> Breach | None:
        """Return the breach, or ``None`` when the difference is acceptable.

        Only over-statement is measured: ``actual`` below ``expected`` returns
        ``None`` here and is reported separately as informational.
        """
        variance = (actual - expected).quantize(SCALE)
        if variance <= ZERO:
            return None
        if variance < self.absorb_below:
            return None

        pct = (variance / expected * HUNDRED).quantize(SCALE) if expected > ZERO else None
        over_relative = pct is not None and pct > self.percentage
        over_absolute = variance > self.absolute_cap

        # An expected value of zero has no meaningful percentage, so the ceiling
        # is the only instrument left. Billing against something that was never
        # ordered is exactly that case, and it must not be absorbed by a
        # division nobody can perform.
        if not over_relative and not over_absolute:
            return None

        return Breach(
            variance=variance,
            variance_pct=pct,
            expected=expected,
            actual=actual,
            tripped_percentage=over_relative,
            tripped_cap=over_absolute,
        )


@dataclass(frozen=True, slots=True)
class Breach:
    """What was exceeded, and by how much.

    Carried into the exception record rather than recomputed for display: a
    reviewer's first question is always "by how much?", and a number derived
    twice is a number that eventually disagrees with itself.
    """

    variance: Decimal
    variance_pct: Decimal | None
    expected: Decimal
    actual: Decimal
    tripped_percentage: bool
    tripped_cap: bool


@dataclass(frozen=True, slots=True)
class TolerancePolicy:
    """The full set of thresholds, versioned as one unit.

    Versioned as a set rather than per-threshold because the thresholds are not
    independent: loosening price while tightening quantity is a different policy
    with different behaviour, and only the combination explains a decision.
    """

    version: str

    #: Unit price, invoice against purchase order.
    price: Tolerance
    #: Billed quantity against received quantity, in units rather than money.
    quantity: Tolerance
    #: Document arithmetic -- each line against its own quantity times price,
    #: the lines against the subtotal (D-039), and subtotal plus tax against the
    #: stated total.
    total: Tolerance
    #: Below this, a field is not trustworthy enough to decide on. Compared
    #: against the *minimum* confidence across the fields a decision reads, never
    #: a mean: an average hides one unreadable field behind four clear ones, and
    #: the unreadable one is the one that matters.
    #:
    #: **Derived, and the evidence is reproducible.** It was 0.50 and
    #: provisional for three milestones, because the deterministic tier's
    #: confidences are ordinal rather than calibrated: 0.88 means "found next to
    #: its label" and 0.62 means "found on the following line", and neither was
    #: measured against how often those readings are actually right.
    #:
    #: `make eval` now measures it. Over 3000 synthetic invoices the value is
    #: still not a probability -- it ranks at AUC 0.99 and calibrates at ECE
    #: 0.19, which is what an ordinal score wearing probability clothing looks
    #: like -- but the question a gate asks is narrower than "is this number a
    #: probability". It asks how often the invoices *above the line* are read
    #: correctly, and that is measurable whether or not the scale means
    #: anything. See D-027 for the derivation and its limits.
    minimum_field_confidence: Decimal

    def __post_init__(self) -> None:
        if not ZERO <= self.minimum_field_confidence <= Decimal("1"):
            raise ValueError("minimum_field_confidence is a probability in [0, 1]")


#: The policy in force.
#:
#: The numbers are chosen to be defensible rather than round. Half a cent
#: absorbs the rounding that any invoice with per-unit pricing will produce.
#: Two percent on price is roughly where a real AP function stops treating a
#: change as clerical and starts treating it as a renegotiation. Quantity
#: absorbs nothing below a whole unit -- fractional over-delivery is a units-of-
#: measure error rather than a rounding artefact -- and caps at five units,
#: because past that it is a different shipment rather than a miscount.
DEFAULT_POLICY: Final = TolerancePolicy(
    version="2026.09.2",
    price=Tolerance(
        absorb_below=Decimal("0.005"),
        percentage=Decimal("2"),
        absolute_cap=Decimal("50"),
    ),
    quantity=Tolerance(
        absorb_below=Decimal("1"),
        percentage=Decimal("2"),
        absolute_cap=Decimal("5"),
    ),
    total=Tolerance(
        absorb_below=Decimal("0.005"),
        percentage=Decimal("0.5"),
        absolute_cap=Decimal("25"),
    ),
    # Derived, not chosen. Over 3000 corpus invoices the gate's own input
    # takes four values; every document at 0.616 or above was read correctly
    # (456 of 456, Wilson lower bound 0.9941 at 99%), and every one of the
    # twelve at 0.580 was wrong. So the evidence identifies an interval,
    # (0.580, 0.616], inside which every threshold accepts exactly the same
    # invoices and carries exactly the same measured risk.
    #
    # 0.60 rather than 0.616 because the interval is what was measured and a
    # point inside it is what should be written down. 0.616 is an artefact of
    # this extractor's arithmetic -- a date found on the following line scores
    # 0.88 x 0.7 -- so a threshold pinned to it would flip on a change that did
    # not alter the evidence at all. 0.60 has headroom on both sides.
    #
    # The old 0.50 was not merely unvalidated: it admitted the 0.580 bucket, so
    # it auto-approved invoices that were measurably always wrong.
    minimum_field_confidence=Decimal("0.60"),
)
