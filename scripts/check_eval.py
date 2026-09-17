"""The extraction regression gate.

    make eval-check        compare against the committed baseline
    make eval-baseline     write a new one, deliberately

Runs the evaluation and compares it line for line with `evals/baseline.txt`.
Exact equality, because the evaluation is deterministic and a tolerance band
would absorb nothing except real breakage.

The order matters. `evals.harness.run` refuses to return at all if the corpus
shrank, lost a layout, read nothing correctly or read everything correctly — so
by the time a comparison happens, the numbers are known to have come from a real
measurement. A gate that compared first could be green against a baseline
regenerated from an evaluation that had quietly stopped evaluating.

WHAT THIS GATE DOES NOT COVER. It scores the five fields that gate a decision,
which is deliberately not every field the extractor reads. Payment details are
the notable omission: `decision_relevant_fields` excludes them because an
unreadable bank line scores 0.0 and would trip the confidence gate on every
invoice that prints no payment block, so the bank signal has its own path
through the exception layer instead (D-016). A change to bank-detail extraction
is therefore invisible here, and is covered by the matching tests. This was
found by mutating that code and watching the gate stay green — which is a
coverage boundary worth knowing rather than a hole worth filling, but only
because it is written down.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from evals.baseline import compare, instrument_changes, render
from evals.harness import DEFAULT_DOCUMENTS, DEFAULT_SEED, run

from threshold.adapters.extraction.deterministic import DeterministicExtractor

BASELINE = Path(__file__).resolve().parents[1] / "evals" / "baseline.txt"


def _measure(documents: int, seed: int) -> str | None:
    try:
        report = asyncio.run(run(DeterministicExtractor(), documents=documents, seed=seed))
    except AssertionError as exc:
        print(f"the evaluation refused to report: {exc}", file=sys.stderr)
        print(
            "This is the harness's own vacuity check, not a comparison failure. "
            "Something stopped the evaluation measuring anything.",
            file=sys.stderr,
        )
        return None
    return render(report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the baseline")
    parser.add_argument("-n", "--documents", type=int, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    current = _measure(args.documents, args.seed)
    if current is None:
        return 1

    if args.write:
        BASELINE.write_text(current, encoding="utf-8", newline="\n")
        print(f"wrote {BASELINE.relative_to(Path.cwd())}")
        print("Read the diff before committing it: every line is a claim about how")
        print("well this system reads invoices, and `shipped.wrong` should only fall.")
        return 0

    if not BASELINE.exists():
        print(f"no baseline at {BASELINE}; create one with `make eval-baseline`", file=sys.stderr)
        return 1

    differences = compare(BASELINE.read_text(encoding="utf-8"), current)
    if not differences:
        print(f"extraction unchanged against the baseline ({args.documents} documents)")
        return 0

    instruments = instrument_changes(differences)
    results = [d for d in differences if d not in instruments]

    # An instrument that moved and changed nothing it measures is not a
    # regression, and failing on it makes this gate cry wolf.
    #
    # `pypdf` and `reportlab` are declared with lower bounds rather than pinned,
    # for a reason the baseline's own docstring argues: pinning a production
    # dependency exactly, to protect a test fixture, freezes it against security
    # updates. The consequence went unnoticed until this ran somewhere other
    # than the machine that wrote the baseline. CI resolved pypdf 6.19.0 against
    # a baseline recorded at 6.16.2, every extraction metric was identical, and
    # the gate failed anyway -- red on every commit, for a library bump that
    # demonstrably changed nothing about how this system reads an invoice.
    #
    # So the version lines are treated as what they are: metadata recorded so a
    # bump shows up in the diff, not measurements of the extractor. They are
    # reported and they never fail on their own. The moment a *result* moves
    # they matter again -- and the message below says so, because a bump that
    # coincides with a changed reading is the case where you cannot tell which
    # caused what.
    if not results:
        print(
            f"extraction unchanged against the baseline ({args.documents} documents), "
            "measured with a different instrument:"
        )
        for difference in instruments:
            print(difference)
        print(
            "\nEvery reading is identical, so the upgrade did not change what this\n"
            "system reads. Refresh the recorded versions with `make eval-baseline`\n"
            "when convenient; nothing is wrong."
        )
        return 0

    print(f"\n{len(differences)} metric(s) moved against evals/baseline.txt:\n", file=sys.stderr)
    for difference in differences:
        print(difference, file=sys.stderr)

    if instruments:
        print(
            "\nOne of the libraries that renders or reads the corpus has changed version.\n"
            "The instrument moved, so every other difference here is a measurement under\n"
            "new conditions rather than a change in the extractor. Read them that way --\n"
            "and note that this is the case the gate cannot resolve for you: a reading\n"
            "that moved alongside an upgrade could be either.",
            file=sys.stderr,
        )

    print(
        "\nIf this is intended, regenerate with `make eval-baseline` and put the diff in\n"
        "the pull request. `shipped.wrong` counts invoices this system would auto-approve\n"
        "on a misreading; it should only ever go down.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
