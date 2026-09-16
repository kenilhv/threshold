"""What the triage agent is allowed to say, and how it is allowed to say it.

The agent reads an invoice that failed its three-way match and says who should
look at it first. It does not decide anything else, and this module is where
that is made true rather than promised.

## The agent does not write

`ARCHITECTURE.md` has said since M0 that "the agent's only write is an
escalation packet" and that this is "a structural property rather than a filter".
The structure is here: the agent produces an `EscalationPacket` and hands it
back. It does not persist anything. The service that called it validates the
packet and decides what to do with it, exactly as it would with a form submitted
by an untrusted client -- because that is what it is.

## The agent is not the router

This is the part the red-team pass changed, and it is worth stating plainly
because the first version of this module got it wrong.

Routing already happened. `match_exception` rows carry `owner_role` and `due_at`,
stamped from the frozen taxonomy at the moment the decision was made. Every
exception this system can raise lands on a named desk with a clock on it before
any agent runs. So the agent is not deciding who is responsible; it is saying
which of the people already involved should look first, and why.

That reframing is what closes the attack the first design left open. A packet
whose `owner` was merely "one of the four roles" let a captured model send a
payment-redirect exception to an accounts-payable clerk -- inside the
vocabulary, past every check, and wrong. Two rules make that a validation error
instead:

* **Grounding.** Every cited code must be one actually recorded against this
  match result. The agent cannot invent an exception, or import one from another
  invoice.
* **Implication.** The owner must be the recorded owner of at least one cited
  code. The agent cannot name a desk that this invoice's own controls do not
  implicate.

The evidence is `approval.OpenException`, the type that already answers "what is
open on this invoice and who owns it" -- the same rows, loaded by the same
`ApprovalService.open_exceptions`, that decide whether a *human* may approve.
Sharing it is the point rather than a convenience: an escalation and an approval
are two things done about one set of open exceptions, and a second, parallel
notion of "what is open" would be a second thing to keep in step.

That type carries the owner from the `match_exception` row rather than looking
it up, and this module depends on that. `models.py` stamps ownership onto the row
because routing rules change and a report that re-derives them would silently
restate who was responsible last quarter -- `spec_for` has exactly one caller in
`src/`, at the moment an exception is raised, and this is not a second one.
Re-deriving here would reintroduce exactly that drift, in the one place where
the question is "was this escalation legitimate at the time".

The blast radius of a completely successful injection is therefore: *reordering
the priority of people who were already going to be involved*, and writing them
a sentence. Nothing is unassigned, nothing loses its clock, nothing is approved.

## The summary is the part that stays dangerous

One field is free text, because a human being needs to read a sentence rather
than an enum. That sentence is written by a model whose input included a PDF
somebody else controlled -- `matching.py` interpolates the supplier's own line
descriptions into `match_exception.detail`, and the agent reads those -- so it
is attacker-influenced by construction and no amount of validation changes that:
you cannot escape natural language.

What can be done is refuse to let it *pretend* to be anything else. `AgentText`
holds it, and its ``__str__`` deliberately does not return it. An f-string that
interpolates one by accident -- into a log line, a metric label, a trace
attribute, a system prompt for the next call -- produces ``<agent text, 412
chars>`` rather than the payload. Reading the content requires saying
``.untrusted``, which is a word a reviewer notices.

That is containment rather than sanitisation, and the distinction is the honest
one: the text still reaches a person, and a person can still be persuaded by it,
or paste it into a reply to the supplier who wrote it. The control for that is
in the console, which must render it as something the agent said rather than as
something the system knows. `tests/unit/test_agent_red_team.py` asserts the
bound; it does not claim the risk is gone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from threshold.domain.approval import OpenException
from threshold.domain.taxonomy import ExceptionCode, OwnerRole

#: Long enough for a paragraph a person will actually read, short enough that a
#: model cannot use the field as a data channel. A summary that runs to
#: kilobytes is not a summary; it is either a malfunction or a payload.
MAX_SUMMARY_CHARS: Final = 600


class EscalationRefusedError(ValueError):
    """The agent produced something outside the vocabulary it is allowed.

    Deliberately an error rather than a coerced value. Silently repairing a
    malformed escalation would hide the one signal that says the model is
    behaving strangely, which is the signal most worth keeping.
    """


@dataclass(frozen=True, slots=True)
class AgentText:
    """Free text written by the model. Attacker-influenced by construction.

    ``__str__`` does not return the content, and that is the entire point. The
    ways this text leaks are all accidents of convenience -- an f-string in a log
    line, a metric attribute, a trace span, the prompt for the next call -- and
    every one of them goes through ``str()``. Making that path produce a
    description instead of the payload turns a silent leak into a visibly empty
    one.
    """

    untrusted: str

    def __str__(self) -> str:
        return f"<agent text, {len(self.untrusted)} chars>"

    def __repr__(self) -> str:
        return self.__str__()


def owners_implicated(recorded: tuple[OpenException, ...]) -> frozenset[OwnerRole]:
    """Who this invoice already routes to, agent or no agent.

    The whole of the system's routing, expressed as a function of rows written
    before any model ran. `test_agent_red_team.py` asserts this is identical
    whether the agent escalated, escalated wrongly, or said nothing at all --
    which is what makes suppression a no-op rather than an outage.
    """
    return frozenset(exception.owner for exception in recorded)


@dataclass(frozen=True, slots=True)
class EscalationPacket:
    """A request for a named person's attention. The agent's only output.

    Not a decision, and not a reassignment. Nothing downstream may treat this as
    an approval, and nothing downstream rewrites `match_exception.owner_role`
    from it -- the packet says who should look *first*, not who is responsible.
    """

    match_result_id: uuid.UUID
    owner: OwnerRole
    codes: tuple[ExceptionCode, ...]
    summary: AgentText
    #: What was actually recorded against this match result. Carried on the
    #: packet rather than checked by a separate call, so that a caller reaching
    #: for the constructor cannot skip the grounding rules -- and so a reviewer
    #: can see which recorded exceptions the agent chose not to cite.
    grounded_in: tuple[OpenException, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.owner, OwnerRole):
            raise EscalationRefusedError(f"owner {self.owner!r} is not one of the four roles")
        if not self.codes:
            raise EscalationRefusedError("an escalation must say which control fired")
        for code in self.codes:
            if not isinstance(code, ExceptionCode):
                raise EscalationRefusedError(f"code {code!r} is not in the frozen taxonomy")
        if len(self.codes) != len(set(self.codes)):
            raise EscalationRefusedError("the same code twice says nothing twice")
        if not self.summary.untrusted.strip():
            raise EscalationRefusedError("an escalation with no summary wastes the reader's time")
        if len(self.summary.untrusted) > MAX_SUMMARY_CHARS:
            raise EscalationRefusedError(
                f"summary is {len(self.summary.untrusted)} characters, over the "
                f"{MAX_SUMMARY_CHARS} a person will read; a summary that long is a payload"
            )

        if not self.grounded_in:
            raise EscalationRefusedError(
                "an escalation with no recorded exceptions has nothing to escalate; "
                "a passing invoice is not a thing to send to a person"
            )

        recorded = {exception.code for exception in self.grounded_in}
        invented = [code.value for code in self.codes if code not in recorded]
        if invented:
            raise EscalationRefusedError(
                f"cited {invented} but this match result recorded "
                f"{sorted(code.value for code in recorded)}; an escalation cites the "
                "controls that fired, not the ones it would be convenient to have fired"
            )

        cited = set(self.codes)
        admissible = {row.owner for row in self.grounded_in if row.code in cited}
        if self.owner not in admissible:
            raise EscalationRefusedError(
                f"escalate to {self.owner.value}: the cited controls are owned by "
                f"{sorted(role.value for role in admissible)}. Routing is recorded on the "
                "exception when it is raised; an escalation may reorder attention among "
                "the desks already involved, not move work to a different one"
            )


def build(
    *,
    match_result_id: uuid.UUID,
    owner: str,
    codes: tuple[str, ...],
    summary: str,
    recorded: tuple[OpenException, ...],
) -> EscalationPacket:
    """Turn what a model actually returned into a packet, or refuse.

    Takes strings because that is what comes back from a model: the enums are
    this side of the boundary, never the other. `recorded` does not come from the
    model -- it is read from the database by the caller, which is what makes the
    grounding check evidence rather than self-report.
    """
    try:
        role = OwnerRole(owner)
    except ValueError as exc:
        raise EscalationRefusedError(
            f"escalate to {owner!r}: not one of {[r.value for r in OwnerRole]}"
        ) from exc

    resolved: list[ExceptionCode] = []
    for code in codes:
        try:
            resolved.append(ExceptionCode(code))
        except ValueError as exc:
            raise EscalationRefusedError(f"code {code!r} is not in the frozen taxonomy") from exc

    return EscalationPacket(
        match_result_id=match_result_id,
        owner=role,
        codes=tuple(resolved),
        summary=AgentText(summary),
        grounded_in=recorded,
    )
