"""Turning scored readings into a threshold somebody can defend.

D-014 set the task: the auto-approval gate is 0.50, nobody validated it, and it
"has to be re-derived from the calibration curve rather than tuned until the
demo looks good". This module is the derivation. Four things in it are easy to
get wrong in ways that still produce a confident-looking number.

**A gate selects a set, not a bucket.** The obvious rule — "take the lowest
confidence value whose own accuracy clears the target" — is wrong, and wrong in
the expensive direction. Risk belongs to everything the gate accepts, so the
statistic has to be the *pooled* accuracy of the accepted set. On the measured
curve the per-bucket rule puts the gate at 0.93 and throws away 56 readings that
were all correct, buying nothing. The cumulative rule puts it far lower and
carries the same risk.

**A percentage without a sample size is not evidence.** One bucket held two
readings and reported "100% correct". The normal approximation says its accuracy
is 100% with certainty — its half-width is z*sqrt(p(1-p)/n), which is exactly zero
when p = 1, so it returns [1.000, 1.000] for a sample of two. Every bound here
is Wilson, which does not degenerate, and the gate reads the *lower* bound.

**Binning is a free parameter unless the value is the bin.** Expected
calibration error on this data ranges from 0.102 to 0.196 depending only on how
it is binned, with no datum changing — and coarsening can only shrink it,
because |sum of gaps| <= sum of |gaps|. Merging two adjacent buckets whose errors point in
opposite directions destroyed half the measured miscalibration. One bin per
distinct confidence value removes the choice, and is the maximum over all
binnings: the number nobody can tune.

**Ranking and calibration are different properties and one number cannot report
both.** This signal ranks well and is calibrated badly, which is exactly what an
ordinal score wearing probability clothing looks like. Both are reported. ECE is
deliberately *not* the headline, because it aggregates over buckets the gate
never accepts — on the measured curve the single largest contributor sits below
any defensible threshold and cannot affect auto-approval risk at all.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol


class Observation(Protocol):
    """Anything with a confidence and a verdict.

    A protocol rather than `FieldScore`, because the gate is calibrated at two
    different units and both have to run through the same arithmetic. A *field
    reading* answers "when the extractor says 0.58, how often is it right",
    which is the diagnostic. A *document* answers "when the weakest field on an
    invoice says 0.58, how often is every field on it right", which is the
    question the gate actually asks — `lowest_decision_confidence` is a minimum
    across five fields, and an invoice is wrong if any one of them is.

    Calibrating only the first and setting the gate from it would be measuring a
    more forgiving question than the one being decided.
    """

    @property
    def confidence(self) -> float: ...

    @property
    def correct(self) -> bool: ...


#: One-sided 95%. The gate asks "is the accepted set at least this accurate",
#: which is a one-sided question; spending half the error budget on an upper
#: bound nobody reads would make the lower bound needlessly pessimistic.
Z_95_ONE_SIDED: Final = 1.6448536269514722


def wilson_lower(correct: int, total: int, *, z: float = Z_95_ONE_SIDED) -> float:
    """Lower confidence bound on a proportion, by the Wilson score interval.

    Not the normal approximation, which is degenerate exactly where this is most
    often used: at ``correct == total`` its width collapses to zero and it
    reports certainty from a sample of two. Wilson keeps a sensible bound there
    — two out of two gives roughly 0.42, which is the honest answer.
    """
    if total <= 0:
        return 0.0
    p = correct / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    # Clamped into [0, p]. Both ends are properties of the interval rather than
    # corrections to it -- a lower bound cannot exceed the estimate it bounds,
    # and cannot be negative -- but floating point does not know that: at p = 0
    # the expression evaluates to about 1.1e-17, which then reports a positive
    # lower bound on a bucket where nothing was read correctly.
    return max(0.0, min(p, (centre - spread) / denominator))


@dataclass(frozen=True, slots=True)
class Bucket:
    """Every reading that shared one confidence value."""

    confidence: float
    total: int
    correct: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def lower_bound(self) -> float:
        return wilson_lower(self.correct, self.total)


@dataclass(frozen=True, slots=True)
class Acceptance:
    """What a gate at this threshold would accept, and how good it would be."""

    threshold: float
    accepted: int
    correct: int
    #: Readings the gate would let through that were wrong. The number the whole
    #: exercise exists to bound: each one is an invoice paid on a misreading.
    wrong: int
    #: Share of all readings the gate accepts. The cost side of the trade —
    #: everything else goes to a person.
    coverage: float
    lower_bound: float


def reliability(scores: Iterable[Observation]) -> list[Bucket]:
    """The calibration curve: one bucket per distinct confidence, high first.

    One bin per distinct value, for the reason in the module docstring: it is
    the only binning that is not a choice.
    """
    tally: dict[float, list[int]] = {}
    for score in scores:
        entry = tally.setdefault(round(score.confidence, 4), [0, 0])
        entry[0] += 1
        entry[1] += int(score.correct)
    return [
        Bucket(confidence=value, total=total, correct=correct)
        for value, (total, correct) in sorted(tally.items(), reverse=True)
    ]


def acceptance_table(scores: Iterable[Observation]) -> list[Acceptance]:
    """For every candidate threshold, what the gate would accept there.

    Cumulative from the top: the row for 0.58 describes a gate accepting
    everything at 0.58 and above, because that is what a gate does.
    """
    buckets = reliability(scores)
    population = sum(b.total for b in buckets)
    rows: list[Acceptance] = []
    accepted = correct = 0
    for bucket in buckets:
        accepted += bucket.total
        correct += bucket.correct
        rows.append(
            Acceptance(
                threshold=bucket.confidence,
                accepted=accepted,
                correct=correct,
                wrong=accepted - correct,
                coverage=accepted / population if population else 0.0,
                lower_bound=wilson_lower(correct, accepted),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class DerivedThreshold:
    """A gate, and the evidence for it."""

    target: float
    threshold: float | None
    accepted: int
    correct: int
    coverage: float
    lower_bound: float
    #: What stopped a lower threshold from qualifying. Named so the number is
    #: arguable rather than merely reported.
    limited_by: str


def derive_threshold(scores: Iterable[Observation], *, target: float) -> DerivedThreshold:
    """The lowest gate whose accepted set is at least ``target`` accurate.

    "At least" means the Wilson lower bound clears the target, not the point
    estimate. A point estimate of 1.0 from two readings would otherwise qualify
    for any target whatsoever, which is how a threshold gets derived from noise.

    Returns a threshold of ``None`` when nothing qualifies. That is a real
    answer and it has been the real answer here: on a 300-reading corpus the
    best lower bound available anywhere on the curve was 0.9801, so a target of
    0.99 could not be met by *any* threshold — not because the extractor was
    poor but because the corpus was too small to prove it. The remedy is more
    documents, not a lower target.
    """
    rows = acceptance_table(scores)
    if not rows:
        return DerivedThreshold(target, None, 0, 0, 0.0, 0.0, "nothing was scored")

    qualifying = [row for row in rows if row.lower_bound >= target]
    if not qualifying:
        best = max(rows, key=lambda r: r.lower_bound)
        return DerivedThreshold(
            target=target,
            threshold=None,
            accepted=0,
            correct=0,
            coverage=0.0,
            lower_bound=best.lower_bound,
            limited_by=(
                f"no threshold qualifies; the best lower bound on the curve is "
                f"{best.lower_bound:.4f} at {best.threshold}, which needs a larger corpus "
                f"rather than a lower target"
            ),
        )

    # Lowest qualifying threshold: the most coverage for the same bounded risk.
    chosen = qualifying[-1]
    index = rows.index(chosen)
    if index + 1 < len(rows):
        rejected = rows[index + 1]
        limited_by = (
            f"admitting {rejected.threshold} would take the lower bound to "
            f"{rejected.lower_bound:.4f}"
        )
    else:
        limited_by = "every reading is accepted"

    return DerivedThreshold(
        target=target,
        threshold=chosen.threshold,
        accepted=chosen.accepted,
        correct=chosen.correct,
        coverage=chosen.coverage,
        lower_bound=chosen.lower_bound,
        limited_by=limited_by,
    )


def auc(scores: Sequence[Observation]) -> float:
    """How well the signal RANKS: P(a correct reading outscores a wrong one).

    Ties count half, which is the Mann-Whitney convention and matters here
    because a discrete signal produces a great many of them.

    Reported beside calibration because they are independent properties and one
    number cannot carry both. A signal can rank perfectly and be calibrated
    atrociously — 0.10 for every correct reading and 0.05 for every wrong one
    has an AUC of 1.0 and means nothing as a probability. That is close to what
    this extractor is.
    """
    right = [s.confidence for s in scores if s.correct]
    wrong = [s.confidence for s in scores if not s.correct]
    if not right or not wrong:
        return float("nan")

    concordant = ties = 0
    for a in right:
        for b in wrong:
            if a > b:
                concordant += 1
            elif a == b:
                ties += 1
    return (concordant + ties / 2) / (len(right) * len(wrong))


def expected_calibration_error(scores: Iterable[Observation]) -> float:
    """How far the numbers are from meaning what they say, one bin per value.

    Deliberately not the headline. It weights every bucket by population, so it
    is dominated by readings the gate never accepts: on the measured curve the
    largest single contributor sat below any defensible threshold and could not
    affect auto-approval risk at all. It answers "are these numbers
    probabilities", which is worth knowing and is a different question from "is
    the gate safe".
    """
    buckets = reliability(scores)
    population = sum(b.total for b in buckets)
    if not population:
        return 0.0
    return sum(b.total * abs(b.accuracy - b.confidence) for b in buckets) / population
