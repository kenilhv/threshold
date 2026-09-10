"""The fixtures and the seed have to agree, and nothing was checking that.

The golden invoice fixture is not free-standing: it cites a purchase order the
seed creates and prints payment details the seed records. Those couplings are
load-bearing and were, until this file, held together only by a comment.

They had already drifted. `bank_routing_last4` carried the routing code
Northgate *retired* forty days before `SEED_EPOCH`, paired with their current
account -- a combination on no row in the database. Nothing noticed, because
nothing compared the two sources, and it stayed invisible for exactly as long as
no code read the value. The moment payment-detail comparison went live it turned
the golden "clean" invoice into the highest-severity exception in the system.

No database needed: both sides are Python literals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.seed import BANK_HISTORY, PURCHASE_ORDERS, VENDORS
from tests.unit.test_fixture_layouts import build_spec

NORTHGATE = "V-100418"


def _current_bank_row(vendor_number: str) -> dict[str, object]:
    rows = [
        row
        for row in BANK_HISTORY
        if row["vendor_number"] == vendor_number and row["days_ago_to"] is None
    ]
    assert len(rows) == 1, f"{vendor_number} should have exactly one open bank row"
    return rows[0]


class TestPaymentDetails:
    def test_the_fixture_prints_the_vendors_current_account(self) -> None:
        """Otherwise the clean path raises BANK_DETAILS_CHANGED on every run."""
        current = _current_bank_row(NORTHGATE)
        assert build_spec().bank_iban_last4 == str(current["account"])[-4:]

    def test_the_fixture_prints_the_vendors_current_routing_code(self) -> None:
        """This is the one that had drifted."""
        current = _current_bank_row(NORTHGATE)
        assert build_spec().bank_routing_last4 == str(current["routing"])[-4:]

    def test_the_fixture_does_not_print_a_retired_account(self) -> None:
        """A stricter statement of the same thing, and the one that reads clearly.

        The previous values were individually plausible -- each appeared
        somewhere in Northgate's history -- and only wrong as a pair.
        """
        spec = build_spec()
        printed = (spec.bank_iban_last4, spec.bank_routing_last4)
        retired = {
            (str(row["account"])[-4:], str(row["routing"])[-4:])
            for row in BANK_HISTORY
            if row["vendor_number"] == NORTHGATE and row["days_ago_to"] is not None
        }
        assert printed not in retired


class TestPurchaseOrder:
    def test_the_fixture_cites_a_seeded_purchase_order(self) -> None:
        cited = build_spec().po_number
        assert cited in {str(order["po_number"]) for order in PURCHASE_ORDERS}

    def test_every_fixture_line_has_a_matching_order_line(self) -> None:
        """SKU and agreed price both, or the clean path stops being clean."""
        spec = build_spec()
        order = next(o for o in PURCHASE_ORDERS if o["po_number"] == spec.po_number)
        lines = order["lines"]
        assert isinstance(lines, list)
        by_sku = {str(line["sku"]): line for line in lines}

        for item in spec.line_items:
            assert item.sku in by_sku, f"{item.sku} is billed but was never ordered"
            assert str(item.unit_price) == str(by_sku[item.sku]["unit_price"]), (
                f"{item.sku} is billed at a price the order does not agree"
            )

    def test_the_fixture_vendor_is_seeded(self) -> None:
        spec = build_spec()
        names = {row["display_name"] for row in VENDORS} | {row["legal_name"] for row in VENDORS}
        assert spec.vendor_name in names


@pytest.mark.parametrize("vendor", [row["vendor_number"] for row in VENDORS])
def test_every_vendor_has_at_most_one_open_bank_row(vendor: str) -> None:
    """Two rows both claiming to be current would make "changed" unanswerable."""
    open_rows = [
        row for row in BANK_HISTORY if row["vendor_number"] == vendor and row["days_ago_to"] is None
    ]
    assert len(open_rows) <= 1
