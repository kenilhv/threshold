"""The harness, and the two levels at which it can measure the wrong thing.

Both mistakes were made while writing it, and neither would have shown up as an
error — each produced a full, plausible report about a gate the system does not
have.

**The wrong unit.** Field accuracy answers "when the extractor says 0.58, is
that value right". The gate asks something harder: it takes a minimum across
five fields and approves an invoice, so an invoice with four right fields and
one wrong total is a wrong invoice. Deriving the threshold from field accuracy
calibrates a more forgiving question than the one being decided.

**The wrong confidence.** `lowest_decision_confidence` is not what the gate
compares. The matcher drops `vendor_name` when the vendor resolved, because at
that point the identity rests on an independent source rather than on a
heuristic (D-014). Since the extractor scores `vendor_name` 0.45 by
construction, the stored minimum is at most 0.45 on every document — so a
harness calibrating it reports that a gate of 0.50 auto-approves nothing at
all, which is an artefact of the measurement rather than a fact about the
system. That is exactly what the first version reported.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from evals.corpus import VENDORS
from evals.harness import DEFAULT_SEED, _outcome, _resolves, run
from evals.scoring import score

from threshold.adapters.extraction.deterministic import DeterministicExtractor
from threshold.domain.extraction import ExtractedInvoice


def _invoice(**overrides: Any) -> ExtractedInvoice:
    blank = {"value": None, "confidence": 0.0, "bbox": None, "raw_text": None}
    fields: dict[str, Any] = {
        name: dict(blank)
        for name in (
            "document_type",
            "invoice_number",
            "vendor_name",
            "vendor_tax_id",
            "po_number",
            "issue_date",
            "due_date",
            "currency",
            "subtotal",
            "tax_amount",
            "total",
        )
    }
    for name, value in overrides.items():
        fields[name] = {**blank, **value}
    return ExtractedInvoice.model_validate(fields)


class TestVendorResolutionIsSimulatedNotAssumed:
    """Whether it resolves decides what the gate reads, so it has to be modelled
    the way the matcher does it — against a vendor master, never against the
    answer key."""

    def test_a_known_vendor_resolves_by_name(self) -> None:
        name = VENDORS[0][0]
        assert _resolves(_invoice(vendor_name={"value": name}))

    def test_case_and_spacing_do_not_matter(self) -> None:
        """Two layouts print the name in capitals, and the matcher normalises."""
        name = VENDORS[0][0]
        assert _resolves(_invoice(vendor_name={"value": f"  {name.upper()}  "}))

    def test_a_known_tax_id_resolves_even_with_an_unreadable_name(self) -> None:
        """Tax identifier first, exactly as `_resolve_vendor` does it."""
        assert _resolves(
            _invoice(
                vendor_name={"value": "SCAN BATCH O011lO IMG 04397"},
                vendor_tax_id={"value": VENDORS[0][1]},
            )
        )

    def test_an_unknown_vendor_does_not_resolve(self) -> None:
        assert not _resolves(_invoice(vendor_name={"value": "Nobody In The Master"}))

    def test_it_does_not_consult_the_answer_key(self) -> None:
        """The check that keeps this honest.

        Reading vendor A's invoice as vendor B is a *wrong* reading, and it
        still resolves, because a vendor row exists for B. If resolution
        consulted the spec it would fail here — and the harness would be using
        ground truth to decide what the gate sees, which is the one thing an
        evaluation may never do.
        """
        other = VENDORS[1][0]
        assert _resolves(_invoice(vendor_name={"value": other}))


class TestTheGateConfidenceIsNotTheStoredOne:
    def test_a_resolved_vendor_is_dropped_from_the_gate(self) -> None:
        """The 0.45 vendor constant would otherwise cap every document."""
        from evals.corpus import generate

        document = next(iter(generate(1, seed=DEFAULT_SEED)))
        spec = document.spec
        invoice = _invoice(
            vendor_name={"value": spec.vendor_name, "confidence": 0.45},
            invoice_number={"value": spec.invoice_number, "confidence": 0.88},
            total={"value": spec.total, "confidence": 0.93},
            currency={"value": spec.currency, "confidence": 0.93},
            issue_date={"value": spec.issue_date.isoformat(), "confidence": 0.62},
        )
        scores = list(score(document="d", layout="l", spec=spec, invoice=invoice))
        outcome = _outcome(document, scores, invoice)

        assert outcome.resolved
        assert outcome.stored_confidence == pytest.approx(0.45)
        assert outcome.confidence == pytest.approx(0.62), (
            "the gate should see the weakest field that is not the dropped vendor name"
        )

    def test_an_unresolved_vendor_still_gates(self) -> None:
        """A name that resolved to nothing has not been corroborated, so its
        heuristic score stands — and VENDOR_UNKNOWN blocks the invoice anyway."""
        from evals.corpus import generate

        document = next(iter(generate(1, seed=DEFAULT_SEED)))
        spec = document.spec
        invoice = _invoice(
            vendor_name={"value": "Nobody In The Master", "confidence": 0.45},
            invoice_number={"value": spec.invoice_number, "confidence": 0.88},
            total={"value": spec.total, "confidence": 0.93},
            currency={"value": spec.currency, "confidence": 0.93},
            issue_date={"value": spec.issue_date.isoformat(), "confidence": 0.62},
        )
        outcome = _outcome(
            document, list(score(document="d", layout="l", spec=spec, invoice=invoice)), invoice
        )

        assert not outcome.resolved
        assert outcome.confidence == pytest.approx(0.45)

    def test_a_dropped_vendor_name_does_not_make_the_document_wrong(self) -> None:
        """It did not enter the decision. It raises VENDOR_UNKNOWN through the
        exception layer instead, which blocks for a reason a person can read."""
        from evals.corpus import generate

        document = next(iter(generate(1, seed=DEFAULT_SEED)))
        spec = document.spec
        invoice = _invoice(
            # A different real vendor: wrong reading, but it resolves.
            vendor_name={"value": VENDORS[1][0], "confidence": 0.45},
            invoice_number={"value": spec.invoice_number, "confidence": 0.88},
            total={"value": spec.total, "confidence": 0.93},
            currency={"value": spec.currency, "confidence": 0.93},
            issue_date={"value": spec.issue_date.isoformat(), "confidence": 0.62},
        )
        outcome = _outcome(
            document, list(score(document="d", layout="l", spec=spec, invoice=invoice)), invoice
        )

        assert outcome.correct, "the gate read no wrong field"
        assert "vendor_name" in outcome.wrong_fields, "but the reading was still wrong"


class TestTheHarnessRefusesToReportNothing:
    """Each of these would otherwise surface as an improvement: fewer readings,
    with the easy ones surviving, is a higher accuracy."""

    def test_a_real_run_produces_both_units(self) -> None:
        report = asyncio.run(run(DeterministicExtractor(), documents=8))
        assert report.readings == 8 * 5
        assert len(report.outcomes) == 8
        assert set(report.by_field()) == {
            "invoice_number",
            "vendor_name",
            "total",
            "currency",
            "issue_date",
        }

    def test_a_corpus_missing_a_layout_is_refused(self) -> None:
        """Four documents rotate through all four layouts; three cannot."""
        with pytest.raises(AssertionError, match="corpus covered"):
            asyncio.run(run(DeterministicExtractor(), documents=3))
