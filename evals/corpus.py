"""The evaluation corpus: documents whose correct reading is known exactly.

Every document is rendered from an ``InvoiceSpec``, and that spec *is* the answer
key. There is no labelling step and no human transcription, so the ground truth
cannot drift from the document — which is the one thing a hand-labelled corpus
can never promise.

Three properties make this worth measuring against, and each is enforced by a
test rather than by intention.

**The truth must be printable.** An invoice prints money to two decimals. A spec
carrying ``244615.0647`` describes a document that cannot exist, and scoring
against it marks a perfectly correct reading of ``244615.06`` as wrong. That is
not a scorer bug to work around; it is a corpus bug, and it was a real one here —
it made the extractor look 12 points worse than it is before anyone noticed.
Every monetary value this module generates is exact at the printed precision.

**The corpus must be hard.** A corpus the extractor reads perfectly calibrates
nothing: every bucket would be 100% correct, the curve would be a single point,
and the exercise would be theatre with a number attached. This one produces
errors across the confidence range because the layouts genuinely differ — two
languages over one geometry, a thermal scan that degrades its own text, a total
that lands on a later page than the line items. `test_eval_corpus.py` asserts
the errors are still there.

**The corpus must be reproducible.** One master seed derives every document's
seed, and the renderers are byte-deterministic, so a calibration number computed
today can be recomputed in a year and compared. Nothing here is committed as a
PDF: the seed is the artefact.

What this corpus is *not* is a sample of real invoices. It is synthetic, and the
same person wrote the generator and the extractor. D-0xx states plainly what
that bounds, and it bounds a great deal: these numbers describe how the
extractor behaves on documents of this kind, and they are not a claim about
production accuracy.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from seeds.invoice_spec import InvoiceSpec, LineItem

#: The four renderers, exercised in rotation so every layout gets the same share
#: of the corpus. Rotation rather than random choice: a random draw leaves the
#: per-layout sample sizes unequal and different for every corpus size, which
#: makes two evaluation runs harder to compare than they need to be.
LAYOUTS: Final = (
    "continental_tabular",
    "us_service_minimal",
    "scanned_thermal",
    "multipage_credit",
)

#: Money is printed to two decimals, so the truth is generated to two decimals.
#: The database column is NUMERIC(18,4) and the domain carries four, but a
#: document cannot express the last two, and the corpus describes documents.
CENTS: Final = Decimal("0.01")

#: Vendors spanning the tax-identifier countries the layouts switch language on,
#: plus name shapes that stress a name extractor: an ampersand, a legal-form
#: suffix, a "B.V." with periods, and a bare two-word name with no suffix at all.
VENDORS: Final = (
    ("Northgate Supply", "DE811907980"),
    ("Aalsmeer Techniek B.V.", "NL004495445B01"),
    ("Cobalt Instruments Ltd", "GB123456789"),
    ("Pallas & Roe Industrieservice GmbH", "DE289201564"),
    ("Vance Redistribution", "US94-3112871"),
)

CURRENCIES: Final = ("EUR", "USD", "GBP")

#: Zero-rated, reduced, German standard, Dutch standard. Zero matters: it makes
#: subtotal equal total, which is the case where the extractor's corroboration
#: boost has nothing to distinguish and the two amounts compete.
TAX_RATES: Final = tuple(Decimal(r) for r in ("0.0000", "0.0700", "0.1900", "0.2100"))

#: One line exercises the degenerate table; twelve and forty push the totals onto
#: a later page in the multipage layout, which is the case a single-page reader
#: silently gets wrong.
LINE_COUNTS: Final = (1, 2, 3, 5, 12, 40)

DESCRIPTIONS: Final = (
    "Hex bolt M8x40 zinc plated, DIN 933, boxed 100",
    "Calibration service, on site, per engineer-hour",
    "Stainless washer A2 M8, bag of 500",
    "Freight surcharge, expedited delivery",
    "Annual maintenance contract, tier 2",
)

#: Prefixes real invoices actually use, including a bare year, which is the one
#: that competes with a date.
NUMBER_PREFIXES: Final = ("INV", "RE", "F", "2026", "AR")


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    """One document, its layout, and the spec that is its answer key."""

    index: int
    layout: str
    spec: InvoiceSpec

    @property
    def name(self) -> str:
        return f"{self.index:04d}-{self.layout}"


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    """A price in cents, exact at the precision a page can print."""
    return (Decimal(rng.randrange(low, high)) / 100).quantize(CENTS)


def _spec_for(rng: random.Random, index: int) -> InvoiceSpec:
    vendor, tax_id = VENDORS[rng.randrange(len(VENDORS))]
    count = LINE_COUNTS[rng.randrange(len(LINE_COUNTS))]

    items: list[LineItem] = []
    subtotal = Decimal("0.00")
    for _ in range(count):
        quantity = Decimal(rng.choice((1, 2, 6, 12, 48)))
        unit_price = _money(rng, 75, 48_000)
        line_total = (quantity * unit_price).quantize(CENTS)
        subtotal += line_total
        items.append(
            LineItem(
                description=DESCRIPTIONS[rng.randrange(len(DESCRIPTIONS))],
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
                unit_of_measure=rng.choice(("EA", "BOX", "HR")),
                sku=f"NG-{rng.randrange(1000, 9999)}",
            )
        )

    rate = TAX_RATES[rng.randrange(len(TAX_RATES))]
    tax = (subtotal * rate).quantize(CENTS)
    issued = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 330))

    # Roughly one document in nine is a credit note, which is about their share
    # of a real ledger. Negative totals are not an edge case to be excluded;
    # they are the case where "the biggest number on the page" stops working.
    credit = rng.randrange(9) == 0
    total = -(subtotal + tax) if credit else subtotal + tax

    return InvoiceSpec(
        invoice_number=(
            f"{NUMBER_PREFIXES[rng.randrange(len(NUMBER_PREFIXES))]}-{rng.randrange(1000, 99999)}"
        ),
        vendor_name=vendor,
        vendor_address_lines=("Einsteinstrasse 14", "80333 Muenchen", "Germany"),
        vendor_tax_id=tax_id,
        bill_to_lines=("Threshold Systems Inc.", "440 N 1st St", "San Jose, CA 95112"),
        issue_date=issued,
        due_date=issued + timedelta(days=rng.choice((14, 30, 45, 60))),
        currency=CURRENCIES[rng.randrange(len(CURRENCIES))],
        line_items=tuple(items),
        subtotal=subtotal,
        tax_rate=rate,
        tax_amount=tax,
        total=total,
        payment_terms="Net 30 days from invoice date",
        # Fixed, and not scored. The fixtures never carry a full account number,
        # and the payment-detail comparison is exercised by the matching tests
        # against seeded vendor rows rather than here.
        bank_iban_last4="8291",
        bank_routing_last4="9593",
        # Absent on about a third, because the matcher cannot assume it.
        po_number=None if rng.randrange(3) == 0 else f"PO-{rng.randrange(10000, 99999)}",
        # Derived from the corpus seed rather than drawn, so that a document's
        # rendering depends only on its position in the corpus. Changing the
        # corpus size then does not reshuffle the documents that were already
        # there, and two runs of different sizes stay comparable.
        seed=_document_seed(index),
    )


def _document_seed(index: int) -> int:
    """A stable per-document seed, spread so adjacent documents do not rhyme.

    The renderers draw their language, spacing and label wording from this, and
    consecutive small integers produce visibly similar documents in some of
    them. A large odd multiplier is enough to decorrelate them and keeps the
    mapping from index to seed obvious.
    """
    return (index + 1) * 7919


def generate(count: int, *, seed: int) -> Iterator[CorpusDocument]:
    """The corpus, deterministically, in a fixed order.

    ``seed`` governs the content — vendors, amounts, dates, line counts. Each
    document's *rendering* seed comes from its index instead, so growing the
    corpus appends rather than reshuffles.
    """
    if count < 1:
        raise ValueError("a corpus of no documents measures nothing")

    rng = random.Random(seed)
    for index in range(count):
        yield CorpusDocument(
            index=index, layout=LAYOUTS[index % len(LAYOUTS)], spec=_spec_for(rng, index)
        )


def render(document: CorpusDocument, path: Path) -> bytes:
    """Write the PDF and return its bytes.

    Imported here rather than at module scope so that importing this module
    costs nothing: the layouts pull in reportlab, which is slow to import and
    absent in any environment that only wants to read a corpus definition.
    """
    import importlib

    module = importlib.import_module(f"seeds.layouts.{document.layout}")
    module.render(document.spec, path)
    return path.read_bytes()
