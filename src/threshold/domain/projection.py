"""The review projection: what a reviewer is allowed to see.

`classification.py` has declared a sensitivity on every column since M0 and
described "the review projection" that would consume it. `sensitivity_of()` had
no production caller. This is it.

The rule is *drop, not mask*, and the argument is in `classification.py`: a
masked bank identifier is still a bank identifier to anyone holding a candidate
list, and four digits is a short list. Masking also destroys the reviewer's task
while pretending not to — nobody can judge whether an invoice looks wrong when
every field they need reads `••••`.

Two properties make this trustworthy rather than decorative:

* It is **derived from the mapper**, not from a hand-written allowlist. A column
  added tomorrow is covered today, because the projection walks
  ``__table__.columns`` and asks each one. An allowlist covers the columns
  somebody remembered, which is the opposite of the columns at risk.
* It **fails closed**, because `sensitivity_of()` does. An undeclared column is
  RESTRICTED, so forgetting to classify one hides it rather than leaking it.

Restricted key names are dropped from JSON payloads too. `Extraction.fields` is
one INTERNAL column whose blob carries the payment digits, so a column-level
rule alone would let them through inside a value it was told was safe.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import inspect
from sqlalchemy.orm import DeclarativeBase

from threshold.domain.classification import Sensitivity, sensitivity_of
from threshold.domain.extraction import RESTRICTED_FIELD_KEYS

#: Ascending. A projection at level N keeps everything at or below N.
_RANK: Final[dict[Sensitivity, int]] = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.RESTRICTED: 2,
}


def _scrub(value: Any) -> Any:
    """Remove restricted key names wherever they appear in a JSON payload.

    Recursive because a restricted key nested inside a list of objects is still
    a restricted key, and `Extraction.fields` is nested.
    """
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in RESTRICTED_FIELD_KEYS}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def project(row: DeclarativeBase, *, level: Sensitivity = Sensitivity.INTERNAL) -> dict[str, Any]:
    """A dictionary of the columns a viewer at ``level`` may see.

    ``INTERNAL`` is the default because it is the projection an authenticated
    member of staff gets: everything except the identifiers that would let
    somebody redirect a payment or impersonate a supplier. ``PUBLIC`` is the
    narrower projection for anything leaving the building.
    """
    ceiling = _RANK[level]
    columns = inspect(type(row)).columns
    return {
        column.key: _scrub(getattr(row, column.key))
        for column in columns
        if _RANK[sensitivity_of(column)] <= ceiling
    }


def restricted_columns(model: type[DeclarativeBase]) -> frozenset[str]:
    """Every column on ``model`` that must never leave the internal projection.

    Exposed so tests can assert against the mapper rather than against a list
    somebody maintains by hand.
    """
    return frozenset(
        column.key
        for column in inspect(model).columns
        if sensitivity_of(column) is Sensitivity.RESTRICTED
    )
