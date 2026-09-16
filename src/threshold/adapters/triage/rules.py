"""The deterministic triage tier: no model, no network, no weights.

It is worse than a model would be at the one thing that genuinely needs
language, and it is better at everything else, which is an unfashionable thing
to say and true. It runs in CI on every commit, its output is reproducible from
its version string, and its summary is assembled from the frozen taxonomy -- so
it contains no supplier-controlled text at all. A model tier reading the same
request has read the invoice's own prose and cannot make that claim.

That is the point of shipping this first rather than second. The escalation
path, the validation, the persistence and the console all get exercised by
something that cannot be captured, so when a model tier arrives the only new
variable is the model.

## When it escalates

Only when there is a choice to make: more than one desk is implicated.

An invoice whose exceptions all belong to one owner is already on that owner's
queue, ordered by the deadline stamped when the control fired. An escalation
there would restate the exception list in different words and add a row that
looks like a finding. Returning ``None`` is not a failure to have an opinion; it
is the absence of one, which is a different and more useful thing to record.

## How it chooses

Worst severity first, then the tightest deadline, then the code's own name so
the result is stable. `bank_details_changed` carries a four-hour clock and
`price_variance` forty-eight, and both block -- so on an invoice carrying both,
the redirect leads. That is the whole of the judgment, and it is a sort.

The deadline comes from today's taxonomy rather than from the row. That is the
opposite of what `domain/triage.py` does with ownership, and deliberately:
*who is responsible* is a fact about the moment the exception was raised, while
*what is most urgent now* is an opinion formed now. Ranking last quarter's
exception by last quarter's policy would be answering a question nobody asked.
"""

from __future__ import annotations

from typing import Final

from threshold.adapters.triage.port import TriageRequest
from threshold.domain.approval import OpenException
from threshold.domain.taxonomy import ExceptionCode, Severity, spec_for
from threshold.domain.triage import (
    MAX_SUMMARY_CHARS,
    EscalationPacket,
    build,
    owners_implicated,
)

#: Bumped when the ordering or the wording changes, because both change the
#: output and a stored escalation has to say which version produced it.
RULES_VERSION: Final = "rules-2026.09.2"

_SEVERITY_ORDER: Final = {Severity.BLOCK: 0, Severity.REVIEW: 1, Severity.INFO: 2}


def _urgency(exception: OpenException) -> tuple[int, int, str]:
    """Worst severity, then tightest deadline, then name. Stable by construction."""
    spec = spec_for(exception.code)
    return (_SEVERITY_ORDER[exception.severity], spec.sla_hours, exception.code.value)


def _describe(lead: OpenException, cited: tuple[ExceptionCode, ...], elsewhere: int) -> str:
    """One sentence per idea, assembled from the taxonomy and nothing else.

    Every word here comes from a frozen table in this repository. Nothing the
    supplier wrote reaches it, which is the property that distinguishes this
    tier from a model one and is asserted in `test_triage_rules.py`.
    """
    spec = spec_for(lead.code)
    desk = lead.owner.value.replace("_", " ")
    parts = [
        f"{desk[:1].upper()}{desk[1:]} first: {spec.summary} "
        f"Due in {spec.sla_hours}h, the tightest clock on this invoice."
    ]
    also = [code for code in cited if code != lead.code]
    if also:
        names = ", ".join(code.value.replace("_", " ") for code in also)
        parts.append(f"Also on this desk: {names}.")
    if elsewhere:
        # Agreement matters here more than it looks. This sentence is the one
        # telling a reviewer that finishing their share does not finish the
        # invoice, and prose that reads as machine output gets skimmed.
        noun, verb = ("desk", "holds") if elsewhere == 1 else ("desks", "hold")
        parts.append(f"{elsewhere} other {noun} {verb} exceptions on the same invoice.")

    summary = " ".join(parts)
    # The cap is a domain rule and this tier must not be the thing that trips
    # it. Truncating rather than raising: a long taxonomy summary is a wording
    # problem, not an incident, and the reviewer is one click from the full list.
    return summary if len(summary) <= MAX_SUMMARY_CHARS else summary[: MAX_SUMMARY_CHARS - 1] + "…"


class RuleBasedTriager:
    """Sorts. That is the entire implementation, and it is enough to be useful."""

    @property
    def triager_id(self) -> str:
        return RULES_VERSION

    async def triage(self, request: TriageRequest) -> EscalationPacket | None:
        recorded = request.recorded
        if len(owners_implicated(recorded)) < 2:
            # Nothing to choose between. See the module docstring.
            return None

        lead = min(recorded, key=_urgency)
        cited = tuple(
            sorted(
                (row.code for row in recorded if row.owner is lead.owner),
                key=lambda code: (_SEVERITY_ORDER[spec_for(code).severity], code.value),
            )
        )
        elsewhere = len(owners_implicated(recorded)) - 1

        return build(
            match_result_id=request.match_result_id,
            owner=lead.owner.value,
            codes=tuple(code.value for code in cited),
            summary=_describe(lead, cited, elsewhere),
            recorded=recorded,
        )
