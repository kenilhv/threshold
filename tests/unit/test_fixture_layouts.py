"""Every fixture layout must render, be deterministic, and be readable.

These are the acceptance criteria for the golden set. A fixture generator that
drifts between runs makes every extraction metric computed against it
unreproducible, and a layout that silently ignores the shape it exists to
exercise — a multi-page invoice that renders on one page — is worse than no
fixture at all, because it makes the eval look like it has coverage it does not.
"""

from __future__ import annotations

import hashlib
import importlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader
from seeds.invoice_spec import InvoiceSpec, LineItem

LAYOUTS = [
    "continental_tabular",
    "us_service_minimal",
    "scanned_thermal",
    "multipage_credit",
]


TAX_RATE = Decimal("0.1900")


def build_spec(
    *,
    line_item_count: int = 3,
    currency: str = "EUR",
    credit: bool = False,
    seed: int = 4471,
) -> InvoiceSpec:
    items = tuple(
        LineItem(
            description=f"Hex bolt M8x{40 + i} zinc plated, DIN 933, boxed 100",
            quantity=Decimal("12"),
            unit_price=Decimal("18.4000"),
            line_total=Decimal("220.8000"),
            unit_of_measure="BOX",
            sku=f"NG-0O1{i}l",
        )
        for i in range(line_item_count)
    )
    # Derived from the lines, never stated beside them. Until D-039 this read
    # subtotal 306.80, tax 58.292, total 365.092 against three lines worth
    # 662.40 -- a fixture that disagreed with itself since M1, which the
    # matcher could not see because it never added the lines up. The lines are
    # what PO-44718 is seeded with (test_seed_coupling.py), so the totals moved
    # and the lines did not. Tax is rounded to the cent, as evals/corpus.py
    # does: the spec is the answer key, and the page can only print cents.
    subtotal = sum((item.line_total for item in items), Decimal("0.0000"))
    tax_amount = (subtotal * TAX_RATE).quantize(Decimal("0.01"))
    total = subtotal + tax_amount
    return InvoiceSpec(
        invoice_number="INV-2026-0918",
        vendor_name="Northgate Supply",
        vendor_address_lines=("Einsteinstrasse 14", "80333 Muenchen", "Germany"),
        vendor_tax_id="DE811907980",
        bill_to_lines=("Threshold Systems Inc.", "440 N 1st St", "San Jose, CA 95112"),
        issue_date=date(2026, 7, 14),
        due_date=date(2026, 8, 13),
        currency=currency,
        line_items=items,
        subtotal=subtotal,
        tax_rate=TAX_RATE,
        tax_amount=tax_amount,
        # A credit note negates the total only, as evals/corpus.py does: its
        # lines and subtotal are still the positive amounts being credited.
        total=-total if credit else total,
        payment_terms="Net 30 days from invoice date",
        # Coupled to the seed, the same way po_number is: these are the last
        # four digits of Northgate's CURRENTLY EFFECTIVE bank row in
        # scripts/seed.py -- routing 026009593, account 5510338291. Change one
        # and the other has to move.
        #
        # This previously read "0248", which is the routing code Northgate
        # RETIRED forty days before SEED_EPOCH, paired with their current
        # account. That combination exists on no row, so once payment-detail
        # comparison went live the golden "clean" invoice raised the highest-
        # severity code in the system and every demo scenario collapsed to
        # blocked. test_seed_coupling.py now fails if they drift again.
        bank_iban_last4="8291",
        bank_routing_last4="9593",
        po_number="PO-44718",
        seed=seed,
    )


def render(layout: str, spec: InvoiceSpec, path: Path) -> bytes:
    module = importlib.import_module(f"seeds.layouts.{layout}")
    module.render(spec, path)
    return path.read_bytes()


@pytest.mark.parametrize("layout", LAYOUTS)
class TestEveryLayout:
    def test_renders_a_valid_pdf(self, layout: str, tmp_path: Path) -> None:
        data = render(layout, build_spec(), tmp_path / "out.pdf")
        assert data[:4] == b"%PDF", f"{layout} did not produce a PDF"
        assert len(data) > 1000, f"{layout} produced a suspiciously small file"

    def test_is_deterministic(self, layout: str, tmp_path: Path) -> None:
        """Same spec in, identical bytes out.

        reportlab embeds a creation timestamp and a document id by default, so
        this fails unless the renderer suppresses both. It is the property that
        makes a golden set reproducible.
        """
        spec = build_spec()
        first = render(layout, spec, tmp_path / "a.pdf")
        second = render(layout, spec, tmp_path / "b.pdf")
        assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest(), (
            f"{layout} is not deterministic"
        )

    def test_exposes_a_stable_layout_name(self, layout: str) -> None:
        module = importlib.import_module(f"seeds.layouts.{layout}")
        assert isinstance(module.LAYOUT_NAME, str)
        assert module.LAYOUT_NAME.strip()

    def test_does_not_mutate_the_spec(self, layout: str, tmp_path: Path) -> None:
        spec = build_spec()
        before = (spec.invoice_number, spec.total, spec.line_items)
        render(layout, spec, tmp_path / "out.pdf")
        assert (spec.invoice_number, spec.total, spec.line_items) == before

    def test_carries_the_invoice_number_somewhere_readable(
        self, layout: str, tmp_path: Path
    ) -> None:
        """Scanned layouts may degrade text, but the identifier must survive.

        Without it there is nothing to join an extraction back to its label, and
        the golden set cannot be scored.
        """
        spec = build_spec()
        path = tmp_path / "out.pdf"
        render(layout, spec, path)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        normalised = text.replace(" ", "").replace("\n", "")
        assert spec.invoice_number.replace("-", "") in normalised.replace("-", ""), (
            f"{layout} did not render the invoice number in an extractable way"
        )


class TestMultipageAndCreditNote:
    """The structural edge cases this family exists to produce."""

    def test_many_line_items_break_across_pages(self, tmp_path: Path) -> None:
        spec = build_spec(line_item_count=40)
        path = tmp_path / "multi.pdf"
        render("multipage_credit", spec, path)
        pages = len(PdfReader(str(path)).pages)
        assert pages > 1, (
            f"40 line items rendered on {pages} page(s). The whole point of this "
            "layout family is the totals block landing on a later page than the "
            "first line item."
        )

    def test_totals_are_absent_from_the_first_page(self, tmp_path: Path) -> None:
        """The failure mode a single-page extractor hits.

        If an extractor reads only page 1 it must find no total, which is what
        makes this fixture worth having.
        """
        spec = build_spec(line_item_count=40)
        path = tmp_path / "multi.pdf"
        render("multipage_credit", spec, path)
        reader = PdfReader(str(path))
        first_page = (reader.pages[0].extract_text() or "").lower()
        assert "total due" not in first_page and "amount due" not in first_page

    def test_negative_total_renders_as_a_credit_note(self, tmp_path: Path) -> None:
        spec = build_spec(credit=True)
        assert spec.is_credit_note
        path = tmp_path / "credit.pdf"
        render("multipage_credit", spec, path)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        assert "CREDIT" in text.upper(), "a negative total must render as a credit note"


class TestLayoutsDifferFromEachOther:
    def test_the_same_invoice_looks_different_in_each_layout(self, tmp_path: Path) -> None:
        """Otherwise the golden set has one layout wearing four names, and any
        layout-sensitivity measured against it is meaningless."""
        spec = build_spec()
        digests = {
            layout: hashlib.sha256(render(layout, spec, tmp_path / f"{layout}.pdf")).hexdigest()
            for layout in LAYOUTS
        }
        assert len(set(digests.values())) == len(LAYOUTS), "two layouts produced identical bytes"


class TestTheFixtureAgreesWithItself:
    """The shared fixture is the answer key for every scenario built on it.

    It carried 662.40 of lines against a 306.80 subtotal from M1 until D-039,
    and nothing noticed, because nothing checked: the matcher did not add the
    lines up, and no test asked the fixture whether it did. This asks.
    """

    @pytest.mark.parametrize("count", [1, 3, 40])
    def test_its_lines_subtotal_tax_and_total_reconcile(self, count: int) -> None:
        spec = build_spec(line_item_count=count)
        for item in spec.line_items:
            assert item.quantity * item.unit_price == item.line_total
        assert sum((i.line_total for i in spec.line_items), Decimal("0")) == spec.subtotal
        assert spec.subtotal + spec.tax_amount == spec.total
        assert spec.tax_amount == (spec.subtotal * spec.tax_rate).quantize(Decimal("0.01"))

    def test_a_credit_note_is_the_same_document_negated(self) -> None:
        credit, invoice = build_spec(credit=True), build_spec()
        assert credit.total == -invoice.total
        assert credit.subtotal == invoice.subtotal
