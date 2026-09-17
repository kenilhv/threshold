"""Field sensitivity, declared on the column rather than in a document.

The redaction rule this project cares about is not "hash the sensitive fields".
Hashing a bank account still yields a bank account to anyone holding a candidate
list, and it destroys the reviewer's task at the same time — an outside reviewer
cannot judge whether an invoice looks wrong if the vendor is `a3f9c1`.

So there are three levels and two projections:

  PUBLIC      always safe to render or log.
  INTERNAL    visible to authenticated staff.
  RESTRICTED  never rendered outside the internal projection and never placed in
              a model's context. Dropped, not masked.

Declaring this on the column means the log formatter, the review projection and
the agent's context builder all consume one source of truth instead of three
implementations that drift.

## Identity is a second axis, not a fourth level

A vendor's name, a buyer's email address and a filename a supplier chose are all
INTERNAL: staff must see them, and they name a person or a company. Sensitivity
alone cannot express that, because the thing that makes them different is not
*how* sensitive they are but *what they are for*.

So a column may also be marked `identity=True`, and the agent's projection drops
those. Marked, not hashed: an earlier design for this increment was a keyed HMAC
alias scheme, and it was abandoned after being written down. The reasoning is in
D-036, and the short version is that the consumer it was for does not exist. The
triage agent never reads a vendor row; routing is by role, not by email; and a
filename the supplier chose is an attack channel rather than a fact worth
preserving. For every column the agent actually reads, an alias carries the same
information as dropping the column, and dropping needs no key to leak.

This is a *narrower* claim than the one the docstring used to make. It said
INTERNAL was "pseudonymised for outside review, with ratios preserved so a 54%
overbill still reads as 54%". Nothing did that, and no consumer wanted it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


#: Keys used in SQLAlchemy's ``Column.info`` dict.
SENSITIVITY_KEY: Final = "sensitivity"
IDENTITY_KEY: Final = "identity"


def classify(level: Sensitivity, *, identity: bool = False) -> dict[str, Any]:
    """Column ``info`` payload. Usage: ``mapped_column(..., info=classify(...))``.

    ``identity`` marks a column that names a person or a company. The key is
    omitted entirely when it is false, so the overwhelming majority of columns
    carry exactly what they carried before.
    """
    if identity and level is Sensitivity.PUBLIC:
        raise ValueError(
            "a PUBLIC column cannot be an identity: PUBLIC means safe to render "
            "anywhere, and this axis exists to withhold. Classify it INTERNAL."
        )
    if identity and level is Sensitivity.RESTRICTED:
        raise ValueError(
            "a RESTRICTED column is already dropped from every projection that "
            "matters; marking it an identity as well says nothing new"
        )
    payload: dict[str, Any] = {SENSITIVITY_KEY: level}
    if identity:
        payload[IDENTITY_KEY] = True
    return payload


def is_identity(column: Any) -> bool:
    """Whether this column names a person or a company.

    Defaults to false, unlike `sensitivity_of`, and deliberately: an undeclared
    column is already treated as RESTRICTED and dropped, so there is nothing for
    this flag to fail closed *about*. It narrows what is shown; it never widens.
    """
    info = getattr(column, "info", None) or {}
    return info.get(IDENTITY_KEY) is True


def sensitivity_of(column: Any) -> Sensitivity:
    """Read a column's declared sensitivity.

    Defaults to RESTRICTED. An undeclared field is treated as the most sensitive
    thing it could be, so forgetting to classify a new column fails closed rather
    than silently leaking it into the review projection.
    """
    info = getattr(column, "info", None) or {}
    value = info.get(SENSITIVITY_KEY)
    return value if isinstance(value, Sensitivity) else Sensitivity.RESTRICTED
