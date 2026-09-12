"""Measure the extractor and derive the gate from what it finds.

    make eval                 400 documents, about 15 seconds
    make eval n=3000          the size D-027's derivation used, about two minutes

Prints two calibration curves, and the difference between them is the point. The
FIELD curve is the diagnostic: it says which field is weak and on which layout.
The DOCUMENT curve is what the gate is derived from, because the gate takes a
minimum across five fields and approves an invoice — so an invoice with four
right fields and one wrong total is a wrong invoice.

The threshold is derived twice, at a 95% and a 99% target, because the target is
a policy choice rather than a measurement. The asymmetry that decides it is not
statistical: a wrong auto-approval pays a wrong invoice, and a wrong escalation
costs a person about two minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from evals.calibration import (
    Acceptance,
    Observation,
    acceptance_table,
    auc,
    derive_threshold,
    expected_calibration_error,
    reliability,
)
from evals.harness import DEFAULT_DOCUMENTS, DEFAULT_SEED, Report, run

from threshold.adapters.extraction.deterministic import DeterministicExtractor

TARGETS = (0.95, 0.99)


def _curve(title: str, observations: Sequence[Observation], explain: str) -> None:
    """One row per distinct confidence: the bucket, and the gate it implies."""
    print(f"\n{title}")
    print(f"  {explain}")
    print(
        f"  {'conf':>6} {'n':>6} {'right':>6} {'acc':>7}  | {'accept':>7} {'wrong':>6} "
        f"{'cover':>7} {'lower bound':>12}"
    )
    rows: dict[float, Acceptance] = {row.threshold: row for row in acceptance_table(observations)}
    for bucket in reliability(observations):
        row = rows[bucket.confidence]
        print(
            f"  {bucket.confidence:>6.3f} {bucket.total:>6d} {bucket.correct:>6d} "
            f"{bucket.accuracy:>7.1%}  | {row.accepted:>7d} {row.wrong:>6d} "
            f"{row.coverage:>7.1%} {row.lower_bound:>12.4f}"
        )


def _report(report: Report) -> None:
    fields = list(report.fields)
    documents = list(report.outcomes)
    right = sum(f.correct for f in fields)
    whole = sum(d.correct for d in documents)

    print(f"\n{report.documents} documents, {report.readings} readings, seed {report.seed}")
    print(f"  field readings correct : {right}/{len(fields)} ({right / len(fields):.1%})")
    print(f"  documents fully correct: {whole}/{len(documents)} ({whole / len(documents):.1%})")
    print(f"  ranking  AUC {auc(fields):.4f} by field, {auc(documents):.4f} by document")
    print(f"  calibration error (one bin per value) {expected_calibration_error(fields):.4f}")
    print("      not the headline: it is dominated by readings the gate never accepts, and")
    print("      it answers 'are these probabilities' rather than 'is the gate safe'")

    print("\n  by field:")
    for name, (total, correct) in report.by_field().items():
        print(f"    {name:<16} {correct:>5}/{total:<5} {correct / total:>7.1%}")

    print("\n  by layout (field readings):")
    for name, (total, correct) in report.by_layout().items():
        print(f"    {name:<22} {correct:>5}/{total:<5} {correct / total:>7.1%}")

    _curve(
        "FIELD READINGS — diagnostic",
        fields,
        "when the extractor says X, how often is that one value right",
    )
    _curve(
        "DOCUMENTS — what the gate decides",
        documents,
        "when the gate's input is X, how often is every field it read right",
    )

    print("\nDERIVED GATE (from the document curve)")
    for target in TARGETS:
        derived = derive_threshold(documents, target=target)
        if derived.threshold is None:
            print(f"  target {target:.2f}: no threshold qualifies")
            print(f"      {derived.limited_by}")
            continue
        print(
            f"  target {target:.2f}: >= {derived.threshold:.3f}   accepts {derived.accepted} "
            f"documents ({derived.coverage:.1%}), {derived.accepted - derived.correct} wrong, "
            f"lower bound {derived.lower_bound:.4f}"
        )
        print(f"      {derived.limited_by}")

    print(
        "\n  The evidence identifies an interval, not a point: every threshold between the\n"
        "  rejected value and the accepted one takes the same documents. See D-027 for why\n"
        "  the shipped constant sits inside it rather than on its edge."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--documents", type=int, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    try:
        report = asyncio.run(
            run(DeterministicExtractor(), documents=args.documents, seed=args.seed)
        )
    except AssertionError as exc:
        # The harness refuses to report a number computed from nothing. Every
        # one of those failures would otherwise look like an improvement.
        print(f"the evaluation refused to report: {exc}", file=sys.stderr)
        return 1

    _report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
