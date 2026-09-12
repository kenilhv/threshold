"""Run the corpus through the real extractor and report what happened.

Two units are measured, and keeping them apart is the point.

A **field reading** answers "when the extractor says 0.58, how often is that
value right". It is the diagnostic: it says which field is weak and which
layout it is weak on.

A **document** answers the question the gate actually asks. The gate reads
``lowest_decision_confidence``, a minimum across five fields, and auto-approves
the invoice. So the unit of risk is an invoice, and an invoice is wrong if *any*
of its five fields is wrong. That is a harder question than the field-level one
and it gives a different threshold — deriving the gate from field accuracy would
be calibrating a more forgiving question than the one being decided.

Nothing here is cached or persisted. Rendering and extracting four hundred
documents takes about thirteen seconds, which is cheaper than the bookkeeping a
cache would need in order to be trusted.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from evals.corpus import LAYOUTS, VENDORS, CorpusDocument, generate, render
from evals.scoring import SCORED_FIELDS, FieldScore, score
from threshold.adapters.extraction.port import Extractor
from threshold.domain.extraction import ExtractedInvoice
from threshold.domain.matching import normalise

#: Four hundred documents is two thousand field readings, of which roughly forty
#: per cent clear the gate. At that size the Wilson lower bound on a perfectly
#: read accepted set reaches about 0.997, so a target of 0.99 is provable rather
#: than merely unrefuted. Sixty documents could not get past 0.98 whatever the
#: extractor did, which is a statement about the corpus and not about the code.
DEFAULT_DOCUMENTS: Final = 400

#: Fixed. The corpus is a measuring instrument, and an instrument whose scale
#: moves between readings is not one. Changing this invalidates every committed
#: baseline, which is exactly the friction it should have.
DEFAULT_SEED: Final = 17


#: The vendor master, as the eval models it: the vendors the corpus prints.
#:
#: Resolution is simulated rather than mocked away, because whether it succeeds
#: decides what the gate reads. `MatchingService._resolve_vendor` matches on tax
#: identifier first and then on an exact normalised name, against rows in the
#: database; here the same two comparisons run against the corpus's own vendors.
#: Nothing consults the answer key: a reading resolves because it matches a
#: vendor, not because it matches the spec that produced it.
_KNOWN_TAX_IDS: Final = frozenset(tax_id for _, tax_id in VENDORS)
_KNOWN_NAMES: Final = frozenset(normalise(name) for name, _ in VENDORS)


def _resolves(invoice: ExtractedInvoice) -> bool:
    """Would the matcher find a vendor row for this reading?"""
    tax_id = (invoice.vendor_tax_id.value or "").strip()
    if tax_id and tax_id in _KNOWN_TAX_IDS:
        return True
    return normalise(invoice.vendor_name.value or "") in _KNOWN_NAMES


@dataclass(frozen=True, slots=True)
class DocumentOutcome:
    """One invoice, as the auto-approval gate sees it.

    Two confidences, and the difference between them is the whole reason this
    class exists.

    `stored` is `lowest_decision_confidence`: the minimum across all five
    decision-relevant fields, written to the extraction row and never adjusted.

    `gate` is what the matcher actually compares against
    `minimum_field_confidence`. It drops `vendor_name` when the vendor resolved,
    because at that point the identity no longer rests on the extractor's
    heuristic — an independent source agreed (D-014).

    Calibrating the stored value would measure a gate the system does not have.
    The extractor scores `vendor_name` 0.45 by construction, so the stored
    minimum is at most 0.45 on *every* document, and a threshold of 0.50 would
    appear to auto-approve nothing at all. That is an artefact of measuring the
    wrong number, and it is what the first version of this harness reported.
    """

    document: str
    layout: str
    stored_confidence: float
    #: What the gate compares. `confidence` rather than `gate_confidence` so the
    #: `Observation` protocol in `calibration.py` reads it: the gate's input is
    #: the thing being calibrated.
    confidence: float
    resolved: bool
    #: True only if every field the GATE relied on was read correctly. A wrong
    #: `vendor_name` that the gate dropped is not counted here, because it did
    #: not enter the decision — it raises VENDOR_UNKNOWN instead, which blocks
    #: through the exception layer rather than through the confidence gate.
    correct: bool
    wrong_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Report:
    documents: int
    seed: int
    fields: tuple[FieldScore, ...]
    outcomes: tuple[DocumentOutcome, ...]

    @property
    def readings(self) -> int:
        return len(self.fields)

    def by_field(self) -> dict[str, tuple[int, int]]:
        return _tally(self.fields, lambda s: s.field)

    def by_layout(self) -> dict[str, tuple[int, int]]:
        return _tally(self.fields, lambda s: s.layout)

    def documents_by_layout(self) -> dict[str, tuple[int, int]]:
        return _tally(self.outcomes, lambda o: o.layout)


class _Scored(Protocol):
    @property
    def correct(self) -> bool: ...


def _tally[ScoredT: _Scored](
    items: Sequence[ScoredT], key: Callable[[ScoredT], str]
) -> dict[str, tuple[int, int]]:
    """(total, correct) per group, in a stable order."""
    out: dict[str, list[int]] = {}
    for item in items:
        entry = out.setdefault(key(item), [0, 0])
        entry[0] += 1
        entry[1] += int(item.correct)
    return {name: (total, correct) for name, (total, correct) in sorted(out.items())}


def _outcome(
    document: CorpusDocument, scores: Sequence[FieldScore], invoice: ExtractedInvoice
) -> DocumentOutcome:
    resolved = _resolves(invoice)
    # The same subset `_decision_confidence` takes, for the same reason.
    gating = [s for s in scores if not (resolved and s.field == "vendor_name")]

    wrong = tuple(s.field for s in gating if not s.correct)
    return DocumentOutcome(
        document=document.name,
        layout=document.layout,
        stored_confidence=min(s.confidence for s in scores),
        confidence=min(s.confidence for s in gating),
        resolved=resolved,
        correct=not wrong,
        wrong_fields=tuple(s.field for s in scores if not s.correct),
    )


async def run(
    extractor: Extractor,
    *,
    documents: int = DEFAULT_DOCUMENTS,
    seed: int = DEFAULT_SEED,
) -> Report:
    """Render, extract and score the whole corpus."""
    fields: list[FieldScore] = []
    outcomes: list[DocumentOutcome] = []

    with tempfile.TemporaryDirectory(prefix="threshold-eval-") as directory:
        path = Path(directory) / "document.pdf"
        for document in generate(documents, seed=seed):
            data = render(document, path)
            result = await extractor.extract(data, "application/pdf")
            scored = list(
                score(
                    document=document.name,
                    layout=document.layout,
                    spec=document.spec,
                    invoice=result.invoice,
                )
            )
            fields.extend(scored)
            outcomes.append(_outcome(document, scored, result.invoice))

    _refuse_a_vacuous_run(documents, fields, outcomes)
    return Report(documents=documents, seed=seed, fields=tuple(fields), outcomes=tuple(outcomes))


def _refuse_a_vacuous_run(
    documents: int, fields: Sequence[FieldScore], outcomes: Sequence[DocumentOutcome]
) -> None:
    """Fail loudly rather than report a number computed from nothing.

    Every check here corresponds to a way the harness could produce a clean,
    plausible, meaningless report: a corpus that silently shrank, a renderer
    that threw and was swallowed, a scorer that stopped scoring some fields. All
    of them would otherwise show up as an *improvement*, because fewer readings
    with the easy ones surviving is a higher accuracy.
    """
    expected = documents * len(SCORED_FIELDS)
    if len(fields) != expected:
        raise AssertionError(f"expected {expected} readings, scored {len(fields)}")
    if len(outcomes) != documents:
        raise AssertionError(f"expected {documents} documents, scored {len(outcomes)}")

    layouts = {o.layout for o in outcomes}
    if layouts != set(LAYOUTS):
        raise AssertionError(f"corpus covered {sorted(layouts)}, not {sorted(LAYOUTS)}")

    if all(s.correct for s in fields):
        raise AssertionError("nothing was read wrongly; this corpus cannot calibrate a gate")
    if not any(s.correct for s in fields):
        raise AssertionError("nothing was read correctly; the harness is measuring a failure")
