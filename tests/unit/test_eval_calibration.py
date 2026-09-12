"""The calibration statistics, checked against an independent computation.

The dataset below is a real measurement — 300 readings from 60 documents — whose
statistics were computed separately, by different code, including cross-checks
against scipy and sklearn. Reproducing those numbers here is a genuine check of
this implementation rather than a restatement of it: a sign error or an
off-by-one in the Wilson bound would show up immediately, and did.

The rest of the file guards the three decisions that would each produce a
confident-looking and wrong threshold: pooling rather than per-bucket, a lower
bound rather than a point estimate, and one bin per value rather than a binning
somebody chose.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from evals.calibration import (
    acceptance_table,
    auc,
    derive_threshold,
    expected_calibration_error,
    reliability,
    wilson_lower,
)
from evals.scoring import FieldScore

from threshold.domain.tolerance import DEFAULT_POLICY

#: (confidence, readings, correct) as measured over 60 documents. The integer
#: counts are recoverable from the percentages without ambiguity: 46/60 is the
#: only integer consistent with 77% at n=60, since 47 would round to 78%.
MEASURED: tuple[tuple[float, int, int], ...] = (
    (0.93, 60, 60),
    (0.88, 30, 30),
    (0.72, 11, 11),
    (0.62, 15, 15),
    (0.58, 15, 15),
    (0.47, 2, 2),
    (0.45, 60, 46),
    (0.43, 10, 6),
    (0.40, 13, 0),
    (0.37, 24, 0),
    (0.00, 60, 0),
)


def _scores(dataset: tuple[tuple[float, int, int], ...] = MEASURED) -> list[FieldScore]:
    out: list[FieldScore] = []
    for confidence, total, correct in dataset:
        for index in range(total):
            out.append(
                FieldScore(
                    document=f"d{index}",
                    layout="l",
                    field="total",
                    confidence=confidence,
                    correct=index < correct,
                    expected="x",
                    actual="x" if index < correct else "y",
                )
            )
    return out


class TestWilsonBounds:
    def test_it_does_not_report_certainty_from_two_readings(self) -> None:
        """The reason the normal approximation is not used anywhere here.

        Its half-width is z·sqrt(p(1-p)/n), which is exactly zero at p=1, so it
        answers "100%, with certainty" for a sample of two. Wilson says what a
        sample of two can actually support.
        """
        assert wilson_lower(2, 2) == pytest.approx(0.4250, abs=5e-4)

    def test_more_evidence_narrows_it(self) -> None:
        assert wilson_lower(60, 60) == pytest.approx(0.9569, abs=5e-4)
        assert wilson_lower(400, 400) == pytest.approx(0.9933, abs=5e-4)
        assert wilson_lower(2, 2) < wilson_lower(60, 60) < wilson_lower(400, 400)

    def test_an_empty_sample_supports_nothing(self) -> None:
        assert wilson_lower(0, 0) == 0.0

    def test_it_never_exceeds_the_point_estimate(self) -> None:
        for correct, total in ((6, 10), (46, 60), (0, 13), (131, 131)):
            assert wilson_lower(correct, total) <= correct / total


class TestTheCurveReproducesAnIndependentComputation:
    def test_the_dataset_is_the_one_that_was_analysed(self) -> None:
        scores = _scores()
        assert len(scores) == 300
        assert sum(s.correct for s in scores) == 185

    def test_ranking(self) -> None:
        """AUC 0.980353, cross-checked against scipy and sklearn elsewhere."""
        assert auc(_scores()) == pytest.approx(0.980353, abs=1e-6)

    def test_calibration_error_per_value(self) -> None:
        assert expected_calibration_error(_scores()) == pytest.approx(0.195733, abs=1e-6)

    def test_the_signal_ranks_far_better_than_it_calibrates(self) -> None:
        """The diagnosis: an ordinal score wearing probability clothing.

        Confidence 0.45 does not mean 45%. It means "more likely right than
        0.43 and less likely than 0.47", and that is all it has ever meant.
        """
        scores = _scores()
        assert auc(scores) > 0.97
        assert expected_calibration_error(scores) > 0.15


class TestAGateSelectsASetNotABucket:
    """The mistake that would cost real coverage for no reduction in risk."""

    def test_pooling_admits_far_more_than_the_per_bucket_rule(self) -> None:
        scores = _scores()
        derived = derive_threshold(scores, target=0.95)

        per_bucket = [b for b in reliability(scores) if b.lower_bound >= 0.95]
        assert [b.confidence for b in per_bucket] == [0.93], (
            "the per-bucket rule should admit only the largest, best bucket"
        )

        assert derived.threshold == 0.47
        assert derived.accepted == 133
        assert derived.accepted > sum(b.total for b in per_bucket), (
            "pooling must accept more readings than the per-bucket rule"
        )

    def test_the_extra_readings_were_all_correct(self) -> None:
        """So the per-bucket rule would have escalated 73 correct readings."""
        derived = derive_threshold(_scores(), target=0.95)
        assert derived.correct == derived.accepted == 133
        assert derived.lower_bound == pytest.approx(0.9801, abs=5e-4)


class TestThresholdDerivation:
    def test_it_picks_the_lowest_qualifying_threshold(self) -> None:
        """Most coverage for the same bounded risk."""
        derived = derive_threshold(_scores(), target=0.95)
        assert derived.threshold == 0.47
        assert derived.coverage == pytest.approx(133 / 300, abs=1e-6)

    def test_it_names_what_stopped_it_going_lower(self) -> None:
        """A threshold nobody can argue with is a threshold nobody can check."""
        derived = derive_threshold(_scores(), target=0.95)
        assert "0.45" in derived.limited_by
        assert "0.89" in derived.limited_by

    def test_an_unreachable_target_returns_no_threshold(self) -> None:
        """And says the corpus is too small rather than quietly lowering the bar.

        On 300 readings the best lower bound anywhere on the curve is 0.9801, so
        0.99 cannot be met by any threshold. That is a statement about the
        sample size, not about the extractor.
        """
        derived = derive_threshold(_scores(), target=0.99)
        assert derived.threshold is None
        assert derived.lower_bound == pytest.approx(0.9801, abs=5e-4)
        assert "larger corpus" in derived.limited_by

    def test_a_bigger_corpus_makes_a_stricter_target_reachable(self) -> None:
        """The same curve, ten times the evidence. Nothing about the extractor
        changed; the bound did, which is the whole point of reporting a bound."""
        ten_times = tuple((c, n * 10, k * 10) for c, n, k in MEASURED)
        derived = derive_threshold(_scores(ten_times), target=0.99)
        assert derived.threshold == 0.47
        assert derived.lower_bound > 0.99

    def test_nothing_scored_is_not_a_threshold_of_zero(self) -> None:
        """An empty corpus must not derive a gate that accepts everything."""
        derived = derive_threshold([], target=0.95)
        assert derived.threshold is None
        assert derived.accepted == 0


class TestTheAcceptanceTable:
    def test_it_is_cumulative_from_the_top(self) -> None:
        rows = acceptance_table(_scores())
        assert [r.accepted for r in rows[:4]] == [60, 90, 101, 116]
        assert rows[-1].accepted == 300

    def test_it_shows_the_cliff(self) -> None:
        """One place on this curve costs anything, and it is where the gate
        would start admitting the vendor-name heuristic."""
        rows = {r.threshold: r for r in acceptance_table(_scores())}
        assert rows[0.47].lower_bound == pytest.approx(0.9801, abs=5e-4)
        assert rows[0.45].lower_bound == pytest.approx(0.8905, abs=5e-4)
        assert rows[0.47].wrong == 0
        assert rows[0.45].wrong == 14

    def test_coverage_and_risk_move_in_opposite_directions(self) -> None:
        rows = acceptance_table(_scores())
        assert rows[0].coverage < rows[-1].coverage
        assert rows[-1].lower_bound < rows[0].lower_bound


class TestBinningIsNotAFreeParameter:
    def test_one_bin_per_distinct_value(self) -> None:
        buckets = reliability(_scores())
        assert len(buckets) == len(MEASURED)
        assert [b.confidence for b in buckets] == sorted((c for c, _, _ in MEASURED), reverse=True)

    def test_coarsening_can_only_shrink_the_error(self) -> None:
        """Why the binning is fixed at one-per-value: it is the maximum.

        |sum of gaps| <= sum of |gaps|, so merging buckets whose errors point in
        opposite directions cancels them. Merging 0.40 (accuracy below its
        confidence) with 0.43 (above) destroys about half the miscalibration
        between them, and nothing in the data changed.
        """
        pair = ((0.40, 13, 0), (0.43, 10, 6))
        separate = expected_calibration_error(_scores(pair))

        merged_confidence = (0.40 * 13 + 0.43 * 10) / 23
        merged = abs(6 / 23 - merged_confidence)

        assert merged < separate
        assert merged < separate * 0.6, (
            f"merging should destroy most of the signal: {merged:.4f} vs {separate:.4f}"
        )

    def test_the_curve_is_weakly_monotone_but_the_readings_are_not(self) -> None:
        """A caveat worth keeping straight.

        Bucket accuracy never increases as confidence falls, so the curve is
        monotone. That is not the same as the signal ranking every reading
        correctly: AUC is 0.98, not 1.0, because wrong readings at 0.45 outrank
        correct ones at 0.43. "Monotone" describes the curve, not the readings.
        """
        buckets = reliability(_scores())
        accuracies = [b.accuracy for b in buckets]
        assert accuracies == sorted(accuracies, reverse=True)
        assert not math.isclose(auc(_scores()), 1.0)


class TestTheGateSitsWhereTheEvidencePutIt:
    """The link between `make eval` and the constant the system ships.

    Measured over 3000 corpus invoices at a 0.99 target: every document whose
    gate confidence was 0.616 or above was read correctly (456 of 456, Wilson
    lower bound 0.9941), and all twelve at 0.580 were wrong. The evidence
    identifies an interval rather than a point — every threshold in
    (0.580, 0.616] accepts exactly the same invoices — so what has to be true of
    the shipped constant is that it lies inside it.

    Without this, the derivation and the number are two facts in two files with
    nothing holding them together, which is how a control quietly reverts to a
    guess.
    """

    #: The two gate-confidence values the derivation sits between. Both are
    #: values this extractor really emits.
    ALWAYS_WRONG = Decimal("0.580")
    ALWAYS_RIGHT = Decimal("0.616")

    def test_it_rejects_the_bucket_that_was_always_wrong(self) -> None:
        assert DEFAULT_POLICY.minimum_field_confidence > self.ALWAYS_WRONG

    def test_it_accepts_the_bucket_that_was_always_right(self) -> None:
        assert DEFAULT_POLICY.minimum_field_confidence <= self.ALWAYS_RIGHT

    def test_it_is_not_pinned_to_the_boundary_itself(self) -> None:
        """0.616 is an artefact of this extractor's arithmetic -- a date on the
        following line scores 0.88 x 0.7 -- so a threshold equal to it would
        flip on a change that altered no evidence. Headroom on both sides."""
        gate = DEFAULT_POLICY.minimum_field_confidence
        assert gate - self.ALWAYS_WRONG >= Decimal("0.01")
        assert self.ALWAYS_RIGHT - gate >= Decimal("0.01")

    def test_the_old_value_would_not_pass_this(self) -> None:
        """0.50 admitted the bucket that was measurably always wrong, which is
        what made it a decoration rather than a control."""
        assert Decimal("0.50") < self.ALWAYS_WRONG

    def test_changing_the_gate_versions_the_policy(self) -> None:
        """A tolerance is stamped on every decision, so moving one is an event
        with a name rather than an edit. The version moved with it."""
        assert DEFAULT_POLICY.version == "2026.09.2"
