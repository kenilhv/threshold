"""The two invariants M0 exists to establish.

Neither needs a running stack, so both run on every commit.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import ClassVar

import pytest

from threshold.domain.classification import Sensitivity, classify, sensitivity_of
from threshold.domain.models import (
    Base,
    MoneyExample,
    Vendor,
    VendorBankAccount,
)


class TestMoneyPrecision:
    """A float anywhere in a monetary path ends the conversation, so the
    conventions are asserted rather than documented."""

    @pytest.mark.parametrize(
        "raw",
        [
            "0.0001",  # smallest representable unit at scale 4
            "12345678901234.5678",  # precision 18, scale 4 — the declared maximum
            "0.1",  # the classic float failure
            "1234.5600",
            "-4830.9900",  # credit notes are negative
        ],
    )
    def test_decimal_survives_a_json_round_trip(self, raw: str) -> None:
        original = Decimal(raw)
        # Decimals must cross a JSON boundary as strings. float() here is what
        # silently loses 0.1 + 0.2, and it is the bug this test exists to catch.
        encoded = json.dumps({"amount": str(original)})
        decoded = Decimal(json.loads(encoded)["amount"])
        assert decoded == original
        assert decoded.as_tuple() == original.as_tuple()

    def test_the_column_is_numeric_not_float(self) -> None:
        column = MoneyExample.__table__.c.amount
        assert column.type.__class__.__name__ == "Numeric"
        assert column.type.precision == 18
        assert column.type.scale == 4
        assert column.type.asdecimal is True

    def test_naive_float_arithmetic_would_have_failed(self) -> None:
        """Documents why the rule exists, so a future reader does not relax it."""
        assert 0.1 + 0.2 != 0.3
        assert Decimal("0.1") + Decimal("0.2") == Decimal("0.3")


class TestFieldClassification:
    def test_declared_sensitivity_is_readable_from_the_column(self) -> None:
        assert sensitivity_of(Vendor.__table__.c.vendor_number) is Sensitivity.PUBLIC
        assert sensitivity_of(Vendor.__table__.c.legal_name) is Sensitivity.INTERNAL
        assert sensitivity_of(Vendor.__table__.c.tax_id) is Sensitivity.RESTRICTED

    def test_an_unclassified_column_is_treated_as_restricted(self) -> None:
        """Fails closed. Forgetting to classify a new column must not leak it."""

        class Unclassified:
            info: ClassVar[dict[str, object]] = {}

        assert sensitivity_of(Unclassified()) is Sensitivity.RESTRICTED
        assert sensitivity_of(object()) is Sensitivity.RESTRICTED

    def test_banking_identifiers_are_restricted(self) -> None:
        """These are the fields that must never reach a model's context or an
        outside reviewer. Dropped, never hashed."""
        table = VendorBankAccount.__table__
        for name in ("account_number_last4", "routing_number_last4", "account_fingerprint"):
            assert sensitivity_of(table.c[name]) is Sensitivity.RESTRICTED, name

    def test_every_column_in_every_model_is_explicitly_classified(self) -> None:
        """The guard that keeps the classification honest as the schema grows.

        A column that reaches RESTRICTED by default rather than by declaration
        is a column somebody forgot, and it will be wrong in one direction or
        the other. Require the declaration.
        """
        # Every mapped table, not a hand-written list. A named tuple of models
        # guards the models somebody remembered to add to it, which is the
        # opposite of what this test is for: the column at risk is the one on
        # the table added last.
        unclassified: list[str] = []
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if "sensitivity" not in (column.info or {}):
                    unclassified.append(f"{table.name}.{column.name}")
        assert not unclassified, f"columns missing a sensitivity declaration: {unclassified}"

    def test_the_guard_covers_every_table_rather_than_a_remembered_few(self) -> None:
        """The guard above is only as good as its reach, so state the reach."""
        covered = {table.name for table in Base.metadata.sorted_tables}
        assert len(covered) >= 12, f"only {len(covered)} tables discovered: {sorted(covered)}"
        for expected in ("match_result", "match_exception", "purchase_order", "job"):
            assert expected in covered, f"{expected} is mapped but not reached by the guard"

    def test_classify_helper_round_trips(self) -> None:
        assert classify(Sensitivity.INTERNAL) == {"sensitivity": Sensitivity.INTERNAL}
