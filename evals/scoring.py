"""Was that reading correct?

The whole evaluation rests on this file, and every rule in it is a judgement
that can hide an error or invent one. A scorer stricter than the system reports
failures that are not failures; a scorer looser hides the ones that are. Both
produce a calibration curve that looks exactly as convincing as a correct one.

So there is one principle, and every normalisation below is justified by it:

    **Score the reading by whether it would produce the right decision, not by
    whether it matches the spec character for character.**

The consumer decides what counts. Vendor resolution matches on
``func.lower(legal_name)``, so a page that prints ``NORTHGATE SUPPLY`` where the
spec says ``Northgate Supply`` has been read correctly — the system would find
the right vendor row. Currency is uppercased by the matcher before comparison,
so case is not an error there either. Amounts are compared exactly, because the
three-way match compares them exactly and a cent is a real difference.

The two failure modes this file exists to avoid, both of which happened while it
was being written:

**A precision mismatch that is not an error.** The corpus generates money at the
precision a page can print, so the comparison is exact and needs no tolerance.
The earlier version compared a four-decimal spec against a two-decimal page and
scored 40% of correct total readings as wrong.

**Treating "not found" as a different kind of outcome.** A field the extractor
did not find is wrong — the decision cannot be made from it. It is not an
abstention to be excluded from the denominator, because excluding it would
compute accuracy over exactly the readings the extractor felt good about, which
is the number most likely to flatter it. `None` scores as incorrect, and the
confidence attached to it is 0.0, so calibration sees it as the extractor's own
prediction that it failed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from seeds.invoice_spec import InvoiceSpec

from threshold.domain.extraction import ExtractedInvoice

#: The fields the auto-approval gate reads, and therefore the fields whose
#: accuracy decides whether the gate is safe. Taken from the domain rather than
#: restated, so a change there cannot silently leave this measuring the wrong
#: set.
SCORED_FIELDS: Final = ("invoice_number", "vendor_name", "total", "currency", "issue_date")


@dataclass(frozen=True, slots=True)
class FieldScore:
    document: str
    layout: str
    field: str
    confidence: float
    correct: bool
    expected: str
    actual: str | None

    @property
    def found(self) -> bool:
        return self.actual is not None


def _expected(field: str, spec: InvoiceSpec) -> str:
    if field == "invoice_number":
        return spec.invoice_number
    if field == "vendor_name":
        return spec.vendor_name
    if field == "total":
        return str(spec.total)
    if field == "currency":
        return spec.currency
    if field == "issue_date":
        return spec.issue_date.isoformat()
    raise KeyError(f"no expected value defined for {field!r}")


def _matches(field: str, expected: str, actual: object) -> bool:
    """One comparison per field, each tied to how the system consumes it."""
    if actual is None:
        return False
    got = str(actual).strip()

    if field == "vendor_name":
        # `MatchingService._resolve_vendor` compares `func.lower(legal_name)`,
        # so a layout that prints the name in capitals has still been read
        # correctly: the same vendor row is found either way.
        return got.casefold() == expected.casefold()

    if field == "currency":
        # The matcher uppercases before comparing, for the same reason.
        return got.upper() == expected.upper()

    if field == "total":
        # Exact. The three-way match compares amounts exactly and raises an
        # exception on a cent, so a cent is a wrong reading here too. The corpus
        # guarantees the expected value is printable exactly, which is what
        # makes an exact comparison fair rather than pedantic.
        #
        # No guard against an unparseable value, and that is deliberate rather
        # than optimistic: `ExtractedInvoice.total` is typed `Decimal`, so
        # pydantic rejects "not a number" at model validation and it can never
        # arrive here. A try/except was written first and removed once a test
        # proved the branch unreachable — dead defensive code reads as a risk
        # that was considered, when it is a risk that cannot occur.
        return Decimal(got) == Decimal(expected)

    # invoice_number and issue_date: identifiers, compared as written. An
    # invoice number is a key the vendor chose and a date is unambiguous once
    # parsed to ISO, so neither has a normalisation that is not just tolerance.
    return got == expected


def score(
    *, document: str, layout: str, spec: InvoiceSpec, invoice: ExtractedInvoice
) -> Iterator[FieldScore]:
    """One `FieldScore` per decision-relevant field."""
    fields = invoice.decision_relevant_fields()
    missing = set(SCORED_FIELDS) - set(fields)
    if missing:
        # The domain moved and this file did not. Loudly, because the quiet
        # version scores four fields out of five and reports a better number.
        raise AssertionError(f"decision_relevant_fields no longer provides {sorted(missing)}")

    for name in SCORED_FIELDS:
        field = fields[name]
        expected = _expected(name, spec)
        yield FieldScore(
            document=document,
            layout=layout,
            field=name,
            confidence=float(field.confidence),
            correct=_matches(name, expected, field.value),
            expected=expected,
            actual=None if field.value is None else str(field.value),
        )
