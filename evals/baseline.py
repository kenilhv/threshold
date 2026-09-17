"""The committed measurement, and what it means for it to have changed.

`make eval-check` re-runs the evaluation and compares it, line for line, against
`evals/baseline.txt`. Three decisions shape it.

**Exact equality on integer counts, not a tolerance band.** The evaluation is
deterministic by construction: seeded specs, byte-reproducible renderers, exact
comparison against the spec that generated each document. There is no run-to-run
variance for a band to absorb. What a band would absorb is real breakage — at
2000 readings one percent is twenty misread fields, which is a bigger regression
than most of the ones worth catching. Counts are compared as integers and
accuracies are derived for display only, so nothing hinges on float formatting.

**Every count carries its denominator.** `n` is redundant with the corpus size
until the day the corpus silently shrinks, and on that day it is the only thing
in the file that says so. A regression that removes documents otherwise reports
as an *improvement*, because the surviving readings are the easy ones.

**Dependency versions are recorded rather than pinned.** reportlab renders the
corpus and pypdf reads it back, so both are measuring instruments for this gate
and an upgrade genuinely invalidates the baseline. The obvious response is to
pin them exactly; the cost is freezing a production dependency against security
updates, which is too high a price for a test fixture. Instead the versions are
in the file, so a bump appears in the diff and the gate says plainly that a
baseline regenerated across one is a behaviour change under review rather than a
refresh.

That reasoning holds and it was incomplete. It assumed the bump would arrive as
a deliberate upgrade in a pull request, and the first time this gate ran
anywhere but the machine that wrote the baseline, it arrived as CI resolving
`pypdf>=5.1` to a newer release than the laptop had. Every extraction metric was
identical; only the version line moved; the gate failed. A gate that goes red
for a library bump that changed nothing teaches people to ignore it, which costs
more than the bump.

So `check_eval.py` separates the two: a `measured_by.*` line that moves alone is
reported and passes, and any *reading* that moves still fails. The versions are
still recorded, a bump still appears in the diff, and the one case the gate
cannot resolve -- a reading that moved alongside an upgrade -- is called out as
exactly that.

The format is one metric per line rather than JSON, for one reason: a pull
request diff. A changed count is one changed line, and a reviewer can see which
field on which layout moved without expanding anything.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from evals.calibration import Observation, derive_threshold, reliability
from evals.harness import Report
from threshold.domain.tolerance import DEFAULT_POLICY

#: Bumped when the SHAPE of the file changes, so an old baseline is rejected as
#: incomparable rather than diffed line by line against a new layout.
SCHEMA_VERSION: Final = 1

#: The targets the file records a derivation for. Both, because the choice
#: between them is a policy judgement and a reader should see what each costs.
TARGETS: Final = (0.95, 0.99)

#: Instruments, not dependencies. What they do to a PDF decides what the
#: evaluation measures.
MEASURED_BY: Final = ("reportlab", "pypdf")


@dataclass(frozen=True, slots=True)
class Difference:
    key: str
    expected: str
    actual: str

    def __str__(self) -> str:
        return f"  {self.key}\n    baseline: {self.expected}\n    now:      {self.actual}"


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - not installed
        return "absent"


def _counts(observations: Sequence[Observation], prefix: str) -> list[tuple[str, str]]:
    return [
        (f"{prefix}.{bucket.confidence:.4f}", f"n={bucket.total} k={bucket.correct}")
        for bucket in reliability(observations)
    ]


def _shipped_gate(documents: Sequence[Observation]) -> tuple[int, int]:
    """What the gate this repository actually ships would accept.

    The derived threshold is evidence; this is consequence. Recording both means
    a change to `minimum_field_confidence` that nobody re-derived shows up in
    the diff as a moved acceptance count rather than as nothing at all.
    """
    gate = float(DEFAULT_POLICY.minimum_field_confidence)
    accepted = [d for d in documents if d.confidence >= gate]
    return len(accepted), sum(1 for d in accepted if not d.correct)


def render(report: Report) -> str:
    """The baseline file for this report."""
    fields = list(report.fields)
    documents = list(report.outcomes)
    accepted, wrong = _shipped_gate(documents)

    rows: list[tuple[str, str]] = [
        ("schema", str(SCHEMA_VERSION)),
        ("corpus.documents", str(report.documents)),
        ("corpus.seed", str(report.seed)),
        ("corpus.readings", str(report.readings)),
        *((f"measured_by.{name}", _version(name)) for name in MEASURED_BY),
        ("policy.version", DEFAULT_POLICY.version),
        ("policy.gate", str(DEFAULT_POLICY.minimum_field_confidence)),
        (
            "overall.fields",
            f"n={len(fields)} k={sum(f.correct for f in fields)}",
        ),
        (
            "overall.documents",
            f"n={len(documents)} k={sum(d.correct for d in documents)}",
        ),
    ]

    rows += [
        (f"field.{name}", f"n={total} k={correct}")
        for name, (total, correct) in report.by_field().items()
    ]
    rows += [
        (f"layout.{name}", f"n={total} k={correct}")
        for name, (total, correct) in report.by_layout().items()
    ]
    rows += _counts(fields, "curve.field")
    rows += _counts(documents, "curve.document")

    for target in TARGETS:
        derived = derive_threshold(documents, target=target)
        value = "none" if derived.threshold is None else f"{derived.threshold:.4f}"
        rows.append(
            (
                f"derived.{target:.2f}",
                f"threshold={value} accepted={derived.accepted} "
                f"wrong={derived.accepted - derived.correct}",
            )
        )

    # What the shipped constant does, as opposed to what the evidence supports.
    rows.append(("shipped.accepted", str(accepted)))
    rows.append(("shipped.wrong", str(wrong)))

    width = max(len(key) for key, _ in rows)
    body = "\n".join(f"{key.ljust(width)}  {value}" for key, value in rows)
    return f"{_PREAMBLE}\n{body}\n"


def parse(text: str) -> dict[str, str]:
    """Key to value, ignoring comments and blank lines."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("  ")
        out[key.strip()] = value.strip()
    return out


def compare(baseline: str, current: str) -> list[Difference]:
    """Every line that moved, in a stable order.

    Missing and added keys are differences too: a metric that stopped being
    reported is exactly as interesting as one whose value changed, and is the
    shape a silently shrinking evaluation takes.
    """
    expected = parse(baseline)
    actual = parse(current)
    differences: list[Difference] = []
    for key in sorted(set(expected) | set(actual)):
        was = expected.get(key, "(absent)")
        now = actual.get(key, "(absent)")
        if was != now:
            differences.append(Difference(key=key, expected=was, actual=now))
    return differences


def instrument_changes(differences: Iterable[Difference]) -> list[Difference]:
    """Differences that are a changed measuring instrument rather than a result."""
    return [d for d in differences if d.key.startswith("measured_by.")]


_PREAMBLE: Final = """\
# The committed measurement of the deterministic extractor.
#
# Regenerate with:  make eval-baseline
#
# Regenerating is not a way to make `make eval-check` pass. Every line here is a
# claim about how well this system reads invoices, and a diff on any of them is
# a behaviour change that belongs in the pull request description. The two
# questions worth asking before accepting one:
#
#   Did accuracy move in the direction the change intended?
#   Did `shipped.wrong` go up? That line counts invoices this system would
#   auto-approve on a misreading, and it should only ever go down.
#
# `measured_by.*` records the versions of the two libraries that render and read
# the corpus. If one of those moved, the instrument moved: re-read the diff as a
# behaviour change under new conditions rather than as a refresh.
"""
