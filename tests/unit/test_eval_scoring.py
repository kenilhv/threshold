"""The scorer, and the corpus it has to be hard enough to score.

Every rule in `evals/scoring.py` is a judgement that can invent a failure or
hide one, and both produce a calibration curve exactly as convincing as a
correct one. So each normalisation is tested against the reason it exists: the
system's own tolerance. Vendor resolution is case-insensitive, so the scorer is.
The three-way match compares amounts exactly, so the scorer does.

`TestTheCorpusIsHardEnoughToCalibrateAgainst` is the one that matters most. It
runs the real extractor and asserts the corpus still produces errors across a
range of confidences. Without it, an extractor improvement — or a corpus that
drifted easy — would leave every bucket at 100%, the curve a single point, and
the derived threshold whatever the code happened to pick, with no test failing.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from evals.corpus import generate, render
from evals.scoring import SCORED_FIELDS, score
from pydantic import ValidationError

from threshold.adapters.extraction.deterministic import DeterministicExtractor
from threshold.domain.extraction import ExtractedInvoice


def _invoice(**overrides: Any) -> ExtractedInvoice:
    """A minimal extraction, with only the fields under test worth reading."""
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


def _one(spec: Any, invoice: ExtractedInvoice, field: str) -> Any:
    scores = {s.field: s for s in score(document="d", layout="l", spec=spec, invoice=invoice)}
    return scores[field]


@pytest.fixture(scope="module")
def spec() -> Any:
    return next(iter(generate(1, seed=17))).spec


class TestTheScorerMatchesWhatTheSystemAccepts:
    """A scorer stricter than the consumer reports failures that are not
    failures; a looser one hides the failures that are."""

    def test_a_vendor_name_in_capitals_is_correct(self, spec: Any) -> None:
        """Two of the four layouts print it uppercased.

        `MatchingService._resolve_vendor` compares `func.lower(legal_name)`, so
        the same vendor row is found either way and the reading is right.
        """
        result = _one(
            spec, _invoice(vendor_name={"value": spec.vendor_name.upper()}), "vendor_name"
        )
        assert result.correct

    def test_a_different_vendor_is_not_correct(self, spec: Any) -> None:
        """The other half. Case-folding everything would pass the test above
        while accepting any string at all."""
        result = _one(spec, _invoice(vendor_name={"value": "Someone Else Entirely"}), "vendor_name")
        assert not result.correct

    def test_currency_case_does_not_matter(self, spec: Any) -> None:
        result = _one(spec, _invoice(currency={"value": spec.currency.lower()}), "currency")
        assert result.correct

    def test_a_total_out_by_a_cent_is_wrong(self, spec: Any) -> None:
        """The matcher raises an exception on a cent, so a cent is a wrong
        reading here too. No tolerance, because the corpus guarantees the
        expected value is exactly printable."""
        off = spec.total + Decimal("0.01")
        assert not _one(spec, _invoice(total={"value": off}), "total").correct
        assert _one(spec, _invoice(total={"value": spec.total}), "total").correct

    def test_trailing_zeros_on_a_total_are_the_same_number(self, spec: Any) -> None:
        """`Decimal("10.50") != Decimal("10.5")` as strings but not as amounts,
        and a string comparison here would invent failures."""
        padded = Decimal(str(spec.total)) + Decimal("0.000")
        assert _one(spec, _invoice(total={"value": padded}), "total").correct

    def test_an_unparseable_total_cannot_reach_the_scorer(self, spec: Any) -> None:
        """Why `_matches` has no guard against one.

        `ExtractedInvoice.total` is typed `Decimal`, so the value is rejected at
        model validation and never arrives. The scorer had a try/except for this
        until this test showed the branch was unreachable; dead defensive code
        reads as a risk that was considered, when it is a risk that cannot occur.
        """
        with pytest.raises(ValidationError):
            _invoice(total={"value": "not a number"})


class TestNotFoundIsWrong:
    """Excluding unread fields would compute accuracy over exactly the readings
    the extractor felt good about."""

    def test_a_missing_field_scores_incorrect(self, spec: Any) -> None:
        result = _one(spec, _invoice(), "invoice_number")
        assert not result.correct
        assert not result.found

    def test_it_stays_in_the_denominator(self, spec: Any) -> None:
        scores = list(score(document="d", layout="l", spec=spec, invoice=_invoice()))
        assert len(scores) == len(SCORED_FIELDS)


class TestTheScorerFailsLoudlyWhenTheDomainMoves:
    def test_a_lost_field_is_an_error_not_a_smaller_denominator(self, spec: Any) -> None:
        """Silently scoring four fields out of five reports a better number."""

        class Shrunk(ExtractedInvoice):
            def decision_relevant_fields(self) -> dict[str, Any]:
                fields = super().decision_relevant_fields()
                fields.pop("total")
                return fields

        shrunk = Shrunk.model_validate(_invoice().model_dump())
        with pytest.raises(AssertionError, match="no longer provides"):
            list(score(document="d", layout="l", spec=spec, invoice=shrunk))


class TestTheCorpusIsHardEnoughToCalibrateAgainst:
    """The guard against an evaluation that reports a beautiful, empty number.

    Runs the real extractor over a slice of the real corpus. Roughly 25ms a
    document, so a hundred documents is a couple of seconds.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def scores(tmp_path_factory: pytest.TempPathFactory) -> list[Any]:
        directory = tmp_path_factory.mktemp("corpus")
        extractor = DeterministicExtractor()

        async def run() -> list[Any]:
            collected: list[Any] = []
            for document in generate(40, seed=17):
                path = Path(directory) / f"{document.name}.pdf"
                data = render(document, path)
                result = await extractor.extract(data, "application/pdf")
                collected.extend(
                    score(
                        document=document.name,
                        layout=document.layout,
                        spec=document.spec,
                        invoice=result.invoice,
                    )
                )
            return collected

        return asyncio.run(run())

    def test_something_was_actually_scored(self, scores: list[Any]) -> None:
        assert len(scores) == 40 * len(SCORED_FIELDS)

    def test_the_extractor_gets_things_wrong(self, scores: list[Any]) -> None:
        """A corpus read perfectly calibrates nothing: every bucket would be
        100% and the curve would be a single point."""
        wrong = [s for s in scores if not s.correct]
        assert wrong, "the corpus is too easy to calibrate against"

    def test_the_extractor_also_gets_things_right(self, scores: list[Any]) -> None:
        """The other direction. A corpus nothing can read is equally useless,
        and would also look like a working evaluation."""
        assert [s for s in scores if s.correct]

    def test_confidence_spans_a_range(self, scores: list[Any]) -> None:
        """A signal with one value cannot be calibrated, however accurate."""
        distinct = {round(s.confidence, 2) for s in scores}
        assert len(distinct) >= 5, f"only {len(distinct)} distinct confidences: {sorted(distinct)}"

    def test_some_bucket_is_genuinely_uncertain(self, scores: list[Any]) -> None:
        """The heart of it.

        A corpus where every confidence value is either always right or always
        wrong has no boundary to find, so a threshold derived from it is an
        artefact of where the values happen to sit. Calibration needs at least
        one bucket that is neither.
        """
        buckets: dict[float, Counter[bool]] = {}
        for s in scores:
            buckets.setdefault(round(s.confidence, 2), Counter())[s.correct] += 1

        mixed = {
            value: dict(counter)
            for value, counter in buckets.items()
            if counter[True] and counter[False]
        }
        assert mixed, (
            "every confidence value is either always correct or always wrong, so "
            f"there is no boundary to calibrate: {
                {v: dict(c) for v, c in sorted(buckets.items())}
            }"
        )

    def test_errors_are_not_confined_to_one_layout(self, scores: list[Any]) -> None:
        """Otherwise the corpus is three easy layouts and one broken one, and
        the aggregate number says more about the mix than the extractor."""
        layouts_with_errors = {s.layout for s in scores if not s.correct}
        assert len(layouts_with_errors) >= 2, layouts_with_errors
