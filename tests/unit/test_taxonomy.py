"""The taxonomy keeps its promises.

Three of them. Every code routes somewhere, every role receives work, and the
stored strings do not move.
"""

from __future__ import annotations

from threshold.domain.taxonomy import TAXONOMY, ExceptionCode, OwnerRole, spec_for

#: Frozen wire values. These strings are written into `match_exception.code` and
#: will outlive this file: they are what a dashboard groups by, what an SLA
#: report counts, and what a labelled evaluation set keys on. Renaming a member
#: without migrating the rows silently reinterprets history, and the failure is
#: invisible because both the old rows and the new code look perfectly valid.
#:
#: Adding a line here is expected. Changing one should be very hard.
FROZEN_CODES = {
    "no_po_reference",
    "po_not_found",
    "po_closed",
    "vendor_unknown",
    "vendor_on_hold",
    "vendor_mismatch",
    "currency_mismatch",
    "duplicate_suspected",
    "price_variance",
    "quantity_variance",
    "receipt_missing",
    "line_not_on_po",
    "under_billed",
    "arithmetic_mismatch",
    "line_total_mismatch",
    "low_confidence_field",
    "checks_incomplete",
    "bank_details_changed",
}


def test_the_stored_strings_have_not_moved() -> None:
    assert {code.value for code in ExceptionCode} == FROZEN_CODES


def test_every_code_routes_somewhere() -> None:
    """The failure this prevents: an exception that is detected and then orphaned.

    It would be found, recorded, given a severity, shown in a count -- and never
    land in anyone's queue, because nothing said whose it was.
    """
    for code in ExceptionCode:
        spec = spec_for(code)
        assert spec.code is code, "a spec is filed under a different code than it names"
        assert spec.owner in OwnerRole
        assert spec.sla_hours > 0, "an exception with no deadline is a queue that grows"
        assert spec.summary.strip().endswith("."), "summaries are shown to reviewers"


def test_every_owner_receives_work() -> None:
    """A role nothing routes to is an org chart, not a routing table."""
    assert {spec.owner for spec in TAXONOMY.values()} == set(OwnerRole)


def test_the_highest_severity_exception_has_the_shortest_clock() -> None:
    """Bank-detail changes are the shape almost every real AP fraud takes.

    Whatever else is tuned, this one should not sit behind a two-day SLA.
    """
    bank = spec_for(ExceptionCode.BANK_DETAILS_CHANGED)
    assert bank.owner is OwnerRole.TREASURY
    assert bank.sla_hours == min(spec.sla_hours for spec in TAXONOMY.values())
