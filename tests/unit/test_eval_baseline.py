"""The regression gate, and the ways a gate stops gating.

A gate over an evaluation has a specific failure mode that ordinary tests do
not: it can keep passing while the thing it measures quietly stops being
measured. Fewer documents, a scorer that skips a field, a corpus that lost a
layout — each removes readings, and the readings that survive are the easy ones,
so the number goes *up*. A gate comparing only accuracies would report an
improvement and go green.

That is why every count in the file carries its denominator, why the harness
refuses to return before any comparison happens, and why `shipped.wrong` is
recorded: it counts invoices this system would auto-approve on a misreading, and
it is the one line that must only ever fall.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from evals.baseline import (
    SCHEMA_VERSION,
    Difference,
    compare,
    instrument_changes,
    parse,
    render,
)
from evals.harness import DocumentOutcome, Report
from evals.scoring import FieldScore

from threshold.domain.tolerance import DEFAULT_POLICY

COMMITTED = Path(__file__).resolve().parents[2] / "evals" / "baseline.txt"


def _report(*, documents: int = 4, gate_confidence: float = 0.9, correct: bool = True) -> Report:
    fields = tuple(
        FieldScore(
            document=f"d{index}",
            layout=["a", "b", "c", "d"][index % 4],
            field=name,
            confidence=gate_confidence,
            correct=correct,
            expected="x",
            actual="x",
        )
        for index in range(documents)
        for name in ("invoice_number", "vendor_name", "total", "currency", "issue_date")
    )
    outcomes = tuple(
        DocumentOutcome(
            document=f"d{index}",
            layout=["a", "b", "c", "d"][index % 4],
            stored_confidence=0.45,
            confidence=gate_confidence,
            resolved=True,
            correct=correct,
            wrong_fields=() if correct else ("total",),
        )
        for index in range(documents)
    )
    return Report(documents=documents, seed=17, fields=fields, outcomes=outcomes)


class TestTheFileIsReadable:
    def test_it_round_trips(self) -> None:
        parsed = parse(render(_report()))
        assert parsed["schema"] == str(SCHEMA_VERSION)
        assert parsed["corpus.documents"] == "4"
        assert parsed["corpus.seed"] == "17"

    def test_comments_are_not_metrics(self) -> None:
        parsed = parse(render(_report()))
        assert not any(key.startswith("#") for key in parsed)

    def test_every_count_carries_its_denominator(self) -> None:
        """The only thing in the file that reveals a shrinking corpus."""
        parsed = parse(render(_report()))
        for key, value in parsed.items():
            if key.startswith(("field.", "layout.", "curve.", "overall.")):
                assert value.startswith("n="), f"{key} has no denominator: {value}"


class TestComparisonCatchesWhatMatters:
    def test_an_unchanged_run_has_no_differences(self) -> None:
        assert compare(render(_report()), render(_report())) == []

    def test_a_moved_count_is_a_difference(self) -> None:
        differences = compare(render(_report()), render(_report(correct=False)))
        assert differences
        assert any(d.key == "overall.fields" for d in differences)

    def test_a_shrinking_corpus_is_a_difference(self) -> None:
        """The failure that would otherwise look like an improvement: fewer
        documents, the easy readings surviving, a higher accuracy."""
        differences = {
            d.key for d in compare(render(_report(documents=8)), render(_report(documents=4)))
        }
        assert "corpus.documents" in differences
        assert "corpus.readings" in differences
        assert "overall.fields" in differences

    def test_a_metric_that_disappeared_is_a_difference(self) -> None:
        """A line that stopped being reported is as interesting as one that
        changed, and is the shape a shrinking evaluation takes."""
        full = render(_report())
        trimmed = "\n".join(
            line for line in full.splitlines() if not line.startswith("field.currency")
        )
        differences = compare(full, trimmed)
        assert [d.key for d in differences] == ["field.currency"]
        assert differences[0].actual == "(absent)"

    def test_a_new_metric_is_a_difference(self) -> None:
        """So that adding a measurement requires accepting a baseline rather
        than silently widening the file."""
        differences = compare(render(_report()), render(_report()) + "field.new  n=1 k=1\n")
        assert [d.key for d in differences] == ["field.new"]
        assert differences[0].expected == "(absent)"


class TestTheShippedGateIsRecordedNotJustTheDerivedOne:
    """The derived threshold is evidence; the shipped constant is consequence.

    Recording only the derivation would let somebody move
    `minimum_field_confidence` without re-deriving it and see nothing change.
    """

    def test_it_counts_what_the_policy_accepts(self) -> None:
        above = float(DEFAULT_POLICY.minimum_field_confidence) + 0.05
        parsed = parse(render(_report(documents=4, gate_confidence=above)))
        assert parsed["shipped.accepted"] == "4"
        assert parsed["shipped.wrong"] == "0"

    def test_documents_below_the_gate_are_not_accepted(self) -> None:
        below = float(DEFAULT_POLICY.minimum_field_confidence) - 0.05
        parsed = parse(render(_report(documents=4, gate_confidence=below)))
        assert parsed["shipped.accepted"] == "0"

    def test_a_wrong_document_above_the_gate_is_counted(self) -> None:
        """`shipped.wrong` is the line that must only ever fall: each unit is an
        invoice this system would auto-approve on a misreading."""
        above = float(DEFAULT_POLICY.minimum_field_confidence) + 0.05
        parsed = parse(render(_report(documents=4, gate_confidence=above, correct=False)))
        assert parsed["shipped.wrong"] == "4"

    def test_moving_the_gate_moves_the_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The check that makes the previous three worth having.

        A baseline that did not react to the policy constant would let a
        loosened gate through as no diff at all.
        """
        import evals.baseline as baseline

        loosened = DEFAULT_POLICY.__class__(
            version=DEFAULT_POLICY.version,
            price=DEFAULT_POLICY.price,
            quantity=DEFAULT_POLICY.quantity,
            total=DEFAULT_POLICY.total,
            minimum_field_confidence=Decimal("0.10"),
        )
        report = _report(documents=4, gate_confidence=0.3)
        before = parse(render(report))["shipped.accepted"]

        monkeypatch.setattr(baseline, "DEFAULT_POLICY", loosened)
        after = parse(render(report))["shipped.accepted"]

        assert before == "0"
        assert after == "4"


class TestInstrumentChangesAreCalledOut:
    def test_a_renderer_version_change_is_flagged_separately(self) -> None:
        """reportlab draws the corpus and pypdf reads it back. If either moved,
        every other difference is a measurement under new conditions."""
        differences = [
            Difference(key="measured_by.pypdf", expected="6.16.2", actual="7.0.0"),
            Difference(key="field.total", expected="n=400 k=119", actual="n=400 k=130"),
        ]
        assert [d.key for d in instrument_changes(differences)] == ["measured_by.pypdf"]

    def test_ordinary_differences_are_not(self) -> None:
        differences = [Difference(key="field.total", expected="n=400 k=119", actual="n=400 k=130")]
        assert instrument_changes(differences) == []


class TestTheCommittedBaseline:
    """It is checked in, so it is checked."""

    def test_it_exists_and_parses(self) -> None:
        assert COMMITTED.is_file(), "run `make eval-baseline`"
        assert parse(COMMITTED.read_text(encoding="utf-8"))

    def test_it_is_the_current_schema(self) -> None:
        parsed = parse(COMMITTED.read_text(encoding="utf-8"))
        assert parsed["schema"] == str(SCHEMA_VERSION), (
            "the file's shape changed; an old baseline is incomparable, not diffable"
        )

    def test_it_records_the_gate_this_repository_ships(self) -> None:
        """So a change to the policy without a re-derivation shows up here."""
        parsed = parse(COMMITTED.read_text(encoding="utf-8"))
        assert parsed["policy.gate"] == str(DEFAULT_POLICY.minimum_field_confidence)
        assert parsed["policy.version"] == DEFAULT_POLICY.version

    def test_the_shipped_gate_lets_nothing_wrong_through(self) -> None:
        """The claim D-027 makes, as a line in a file a gate compares.

        If this ever reads anything but zero, the system auto-approves invoices
        it misread, and that is a finding rather than a number to update.
        """
        parsed = parse(COMMITTED.read_text(encoding="utf-8"))
        assert parsed["shipped.wrong"] == "0"

    def test_it_accepts_something(self) -> None:
        """A gate that approves nothing has no false approvals either, and would
        satisfy the test above while making the system useless."""
        parsed = parse(COMMITTED.read_text(encoding="utf-8"))
        assert int(parsed["shipped.accepted"]) > 0
