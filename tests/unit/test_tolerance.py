"""Tolerance arithmetic at its boundaries.

The matcher exercises these indirectly, but boundaries are where tolerance bugs
live and where an indirect test says "no exception" without telling you which of
three thresholds decided that.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from threshold.domain.tolerance import DEFAULT_POLICY, Tolerance

D = Decimal

#: floor 1.00, 10%, ceiling 20.00 -- chosen so each threshold is the deciding
#: one somewhere in the range, which is what makes the cases below distinct.
T = Tolerance(absorb_below=D("1.00"), percentage=D("10"), absolute_cap=D("20.00"))


class TestTheFloor:
    def test_a_variance_below_the_floor_is_absorbed(self) -> None:
        assert T.breach(actual=D("100.99"), expected=D("100.00")) is None

    def test_the_floor_gates_but_does_not_trigger(self) -> None:
        """Clearing the floor is necessary, not sufficient.

        A variance of exactly 1.00 against 100.00 clears the floor and then
        trips nothing: 1% is inside the percentage and 1.00 is under the
        ceiling. Reading the floor as a trigger rather than a gate would turn
        every threshold into "1.00 or more is an exception".
        """
        assert T.breach(actual=D("101.00"), expected=D("100.00")) is None

    def test_at_the_floor_and_over_the_percentage_breaches(self) -> None:
        breach = T.breach(actual=D("6.00"), expected=D("5.00"))
        assert breach is not None
        assert breach.variance == D("1.0000")
        assert breach.tripped_percentage

    def test_just_under_the_floor_is_absorbed_however_large_the_percentage(self) -> None:
        """19.8% of the base, and still immaterial in money. The floor wins."""
        assert T.breach(actual=D("5.99"), expected=D("5.00")) is None

    def test_the_floor_beats_a_large_percentage(self) -> None:
        """Ninety percent of a ten-cent part is nine cents, and nobody is paged."""
        assert T.breach(actual=D("0.19"), expected=D("0.10")) is None


class TestThePercentage:
    def test_over_the_percentage_breaches(self) -> None:
        breach = T.breach(actual=D("111.00"), expected=D("100.00"))
        assert breach is not None
        assert breach.tripped_percentage
        assert not breach.tripped_cap
        assert breach.variance == D("11.0000")
        assert breach.variance_pct == D("11.0000")

    def test_exactly_at_the_percentage_does_not_breach(self) -> None:
        assert T.breach(actual=D("110.00"), expected=D("100.00")) is None


class TestTheCeiling:
    def test_a_small_percentage_of_a_large_base_still_breaches(self) -> None:
        breach = T.breach(actual=D("10021.00"), expected=D("10000.00"))
        assert breach is not None
        assert breach.tripped_cap
        assert not breach.tripped_percentage, "0.21% is well inside the percentage"


class TestDegenerateBases:
    def test_a_zero_expected_value_falls_back_to_the_ceiling(self) -> None:
        """Billing for something never ordered has no percentage to compute.

        The division is undefined, so the ceiling is the only instrument left.
        Returning None here would absorb an entire unordered line in silence.
        """
        breach = T.breach(actual=D("30.00"), expected=D("0"))
        assert breach is not None
        assert breach.variance_pct is None
        assert breach.tripped_cap

    def test_a_zero_expected_value_under_the_ceiling_is_still_absorbed(self) -> None:
        assert T.breach(actual=D("5.00"), expected=D("0")) is None

    def test_understatement_is_never_a_breach(self) -> None:
        assert T.breach(actual=D("1.00"), expected=D("1000.00")) is None

    def test_an_exact_match_is_not_a_breach(self) -> None:
        assert T.breach(actual=D("100.00"), expected=D("100.00")) is None


class TestConfiguration:
    def test_a_floor_above_the_ceiling_is_rejected(self) -> None:
        """Otherwise the cap can never fire and the control silently does nothing."""
        with pytest.raises(ValueError, match="absorb_below"):
            Tolerance(absorb_below=D("50"), percentage=D("1"), absolute_cap=D("10"))

    def test_negative_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="magnitudes"):
            Tolerance(absorb_below=D("-1"), percentage=D("1"), absolute_cap=D("10"))

    def test_the_shipped_policy_is_internally_consistent(self) -> None:
        for name in ("price", "quantity", "total"):
            tolerance = getattr(DEFAULT_POLICY, name)
            assert tolerance.absorb_below <= tolerance.absolute_cap
        assert D("0") <= DEFAULT_POLICY.minimum_field_confidence <= D("1")
        assert DEFAULT_POLICY.version
