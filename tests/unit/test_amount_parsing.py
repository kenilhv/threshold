"""The number parser, which is where invoices from two continents collide.

1.234,56 and 1,234.56 are the same amount written by different conventions, and
getting this wrong is the single most common extraction error on non-US
documents. It is also silent: the pipeline does not crash, it just pays a
thousand times too much or a thousand times too little.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from threshold.adapters.extraction.deterministic import _parse_amount


class TestUnambiguousWhenBothSeparatorsPresent:
    """Whichever separator comes last is the decimal separator."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,234.56", "1234.56"),  # US
            ("1.234,56", "1234.56"),  # European
            ("1,234,567.89", "1234567.89"),
            ("1.234.567,89", "1234567.89"),
            ("$1,234.56", "1234.56"),
            ("€1.234,56", "1234.56"),
        ],
    )
    def test_last_separator_wins(self, raw: str, expected: str) -> None:
        assert _parse_amount(raw) == Decimal(expected)


class TestSingleSeparator:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,50", "1.50"),  # European decimal comma
            ("0,99", "0.99"),
            ("1,234", "1234"),  # groups exactly three digits -> thousands
            ("12,345", "12345"),
            ("1234.56", "1234.56"),  # plain US decimal
            ("0.99", "0.99"),
            ("1.234", "1234"),  # ambiguous; read as European thousands
        ],
    )
    def test_disambiguated_by_grouping(self, raw: str, expected: str) -> None:
        assert _parse_amount(raw) == Decimal(expected)


class TestNegatives:
    """Credit notes. Brackets are the accounting convention and a naive parser
    reads them as positive, which inverts the sign of a refund."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("(1,234.56)", "-1234.56"),
            ("(1.234,56)", "-1234.56"),
            ("-1,234.56", "-1234.56"),
            ("- 500.00", "-500.00"),
        ],
    )
    def test_negative_forms(self, raw: str, expected: str) -> None:
        assert _parse_amount(raw) == Decimal(expected)


class TestRejections:
    @pytest.mark.parametrize("raw", ["", "   ", "abc", "N/A", "-", "..", ","])
    def test_unparseable_returns_none_rather_than_zero(self, raw: str) -> None:
        """None and zero are different facts.

        Returning 0 for an unreadable total would let an invoice through the
        auto-approval gate as a free invoice. Absence must stay absence all the
        way to the decision.
        """
        assert _parse_amount(raw) is None


class TestPrecision:
    def test_no_float_anywhere_in_the_path(self) -> None:
        value = _parse_amount("1,234.10")
        assert isinstance(value, Decimal)
        # The canonical float failure. If this parser ever routes through float,
        # this assertion is what catches it.
        assert value == Decimal("1234.10")
        assert value * 3 == Decimal("3702.30")

    def test_four_decimal_places_survive(self) -> None:
        """The schema stores NUMERIC(18,4); unit prices genuinely use them."""
        assert _parse_amount("0.0001") == Decimal("0.0001")
        assert _parse_amount("12.3456") == Decimal("12.3456")

    def test_large_values_are_exact(self) -> None:
        assert _parse_amount("12,345,678,901,234.5678") == Decimal("12345678901234.5678")
