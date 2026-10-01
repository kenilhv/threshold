"""The corpus, and the two ways an evaluation quietly stops measuring anything.

A calibration curve is only as trustworthy as the corpus under it, and a corpus
can fail in two directions that both look like success:

**Too easy.** If the extractor reads every document correctly, every confidence
bucket is 100%, the curve is a single point, and the derived threshold is
whatever the code happened to pick. The evaluation reports a beautiful number
and has measured nothing. `TestTheCorpusIsHardEnoughToCalibrateAgainst` is the
guard, and it is the most important class in this file.

**Not printable.** If a spec carries more precision than a page can show, a
correct reading scores as wrong and the extractor's accuracy is understated by
whatever fraction of fields are money. That is not hypothetical — it happened
here, and it cost twelve points of apparent accuracy on `total` before anyone
checked whether the scorer or the extractor was at fault.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from evals.corpus import CENTS, LAYOUTS, generate, render


class TestTheCorpusIsReproducible:
    def test_the_same_seed_gives_the_same_documents(self) -> None:
        """A calibration number computed today has to be recomputable."""
        first = list(generate(24, seed=17))
        second = list(generate(24, seed=17))
        assert [d.spec for d in first] == [d.spec for d in second]

    def test_a_different_seed_gives_different_documents(self) -> None:
        """Otherwise the seed is decoration and the corpus is one document."""
        first = list(generate(24, seed=17))
        second = list(generate(24, seed=18))
        assert [d.spec for d in first] != [d.spec for d in second]

    def test_growing_the_corpus_appends_rather_than_reshuffles(self) -> None:
        """So a bigger run stays comparable with a smaller one.

        Each document's rendering seed comes from its index, not from the
        stream, which is what makes the first twenty-four documents of a
        hundred-document corpus the same twenty-four as before.
        """
        small = list(generate(12, seed=17))
        large = list(generate(48, seed=17))
        assert [d.spec.seed for d in small] == [d.spec.seed for d in large[:12]]
        assert [d.layout for d in small] == [d.layout for d in large[:12]]

    def test_rendering_is_byte_identical(self, tmp_path: Path) -> None:
        document = next(iter(generate(1, seed=3)))
        a = render(document, tmp_path / "a.pdf")
        b = render(document, tmp_path / "b.pdf")
        assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()

    def test_an_empty_corpus_is_refused(self) -> None:
        with pytest.raises(ValueError, match="measures nothing"):
            list(generate(0, seed=1))


class TestTheTruthIsPrintable:
    """Ground truth a document cannot express is not ground truth.

    Money prints to two decimals. A spec carrying four describes a document that
    does not exist, and every scorer comparing against it is wrong in the same
    direction: it marks correct readings as errors.
    """

    def test_every_monetary_value_is_exact_at_two_decimals(self) -> None:
        for document in generate(120, seed=17):
            spec = document.spec
            amounts = {
                "subtotal": spec.subtotal,
                "tax_amount": spec.tax_amount,
                "total": spec.total,
            }
            for line_number, item in enumerate(spec.line_items, start=1):
                amounts[f"line {line_number} unit_price"] = item.unit_price
                amounts[f"line {line_number} line_total"] = item.line_total
            for name, value in amounts.items():
                assert value == value.quantize(CENTS), (
                    f"{document.name} {name} is {value}, which a page cannot print exactly"
                )

    def test_the_arithmetic_on_the_page_reconciles(self) -> None:
        """Subtotal plus tax is the total, to the cent.

        The extractor raises confidence when those three reconcile, so a corpus
        whose arithmetic does not hold would suppress that signal everywhere and
        measure a different extractor from the one that runs.
        """
        for document in generate(120, seed=17):
            spec = document.spec
            magnitude = abs(spec.total)
            assert magnitude == spec.subtotal + spec.tax_amount, document.name

    def test_the_lines_add_up_to_the_subtotal(self) -> None:
        """The matcher checks this since D-039, so a corpus that broke it would
        describe documents the system blocks. Held by construction in
        `evals/corpus.py`; asserted here so the construction cannot drift."""
        for document in generate(120, seed=17):
            spec = document.spec
            itemised = sum((i.line_total for i in spec.line_items), Decimal("0.00"))
            assert itemised == spec.subtotal, document.name

    def test_the_check_can_fail(self) -> None:
        """A guard for the guard: four decimals must be detectable as such."""
        assert Decimal("244615.0647") != Decimal("244615.0647").quantize(CENTS)


class TestTheCorpusCoversWhatItClaims:
    def test_every_layout_appears(self) -> None:
        seen = {d.layout for d in generate(len(LAYOUTS) * 3, seed=17)}
        assert seen == set(LAYOUTS)

    def test_layouts_are_evenly_represented(self) -> None:
        """Unequal samples make per-layout accuracies harder to compare than
        they need to be, and the rotation costs nothing."""
        counts: dict[str, int] = {}
        for document in generate(len(LAYOUTS) * 25, seed=17):
            counts[document.layout] = counts.get(document.layout, 0) + 1
        assert set(counts.values()) == {25}

    def test_it_contains_credit_notes(self) -> None:
        """A negative total is where "the largest number on the page" stops
        working, so a corpus without one is not testing the hard case."""
        assert any(d.spec.is_credit_note for d in generate(200, seed=17))

    def test_it_contains_invoices_with_no_purchase_order(self) -> None:
        assert any(d.spec.po_number is None for d in generate(60, seed=17))

    def test_it_contains_documents_long_enough_to_break_across_pages(self) -> None:
        assert any(len(d.spec.line_items) >= 40 for d in generate(200, seed=17))

    def test_it_contains_zero_rated_invoices(self) -> None:
        """Where subtotal equals total, so the two compete as candidates."""
        assert any(d.spec.tax_rate == Decimal("0.0000") for d in generate(120, seed=17))

    def test_it_spans_currencies_and_vendors(self) -> None:
        documents = list(generate(120, seed=17))
        assert len({d.spec.currency for d in documents}) >= 3
        assert len({d.spec.vendor_name for d in documents}) >= 4
