"""Field sensitivity, declared on the column rather than in a document.

The redaction rule this project cares about is not "hash the sensitive fields".
Hashing a bank account still yields a bank account to anyone holding a candidate
list, and it destroys the reviewer's task at the same time — an outside reviewer
cannot judge whether an invoice looks wrong if the vendor is `a3f9c1`.

So there are three levels and two projections:

  PUBLIC      always safe to render or log.
  INTERNAL    visible to authenticated staff; pseudonymised for outside review,
              with ratios preserved so a 54% overbill still reads as 54%.
  RESTRICTED  never rendered outside the internal projection and never placed in
              a model's context. Dropped, not masked.

Declaring this on the column means the log formatter, the review projection and
the agent's context builder all consume one source of truth instead of three
implementations that drift.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


#: Key used in SQLAlchemy's ``Column.info`` dict.
SENSITIVITY_KEY: Final = "sensitivity"


def classify(level: Sensitivity) -> dict[str, Any]:
    """Column ``info`` payload. Usage: ``mapped_column(..., info=classify(...))``."""
    return {SENSITIVITY_KEY: level}


def sensitivity_of(column: Any) -> Sensitivity:
    """Read a column's declared sensitivity.

    Defaults to RESTRICTED. An undeclared field is treated as the most sensitive
    thing it could be, so forgetting to classify a new column fails closed rather
    than silently leaking it into the review projection.
    """
    info = getattr(column, "info", None) or {}
    value = info.get(SENSITIVITY_KEY)
    return value if isinstance(value, Sensitivity) else Sensitivity.RESTRICTED
