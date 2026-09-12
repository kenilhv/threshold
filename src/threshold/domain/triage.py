"""What the triage agent is allowed to say, and how it is allowed to say it.

The agent reads an invoice that failed its three-way match and decides who
should look at it. It does not decide anything else, and this module is where
that is made true rather than promised.

## The agent does not write

`ARCHITECTURE.md` has said since M0 that "the agent's only write is an
escalation packet" and that this is "a structural property rather than a filter".
The structure is here: the agent produces an `EscalationPacket` and hands it
back. It does not persist anything. The service that called it validates the
packet and decides what to do with it, exactly as it would with a form submitted
by an untrusted client — because that is what it is.

So there is no approve tool to guard, no ledger write to filter, and no prompt
to harden. A model that has been completely captured by an injected instruction
can return a packet, and a packet is a request for a person's attention.

## The vocabulary is closed

`owner` is one of four roles and `codes` are drawn from the frozen exception
taxonomy. Both are enums, so "escalate to accounts-payable-override" is not a
thing a compromised model can express — it is a validation error before it is
anything else. The blast radius of a successful injection is therefore bounded
to *choosing badly among legitimate options*, which is a much smaller problem
than choosing freely.

## The summary is the part that stays dangerous

One field is free text, because a human being needs to read a sentence rather
than an enum. That sentence is written by a model whose input included a PDF
somebody else controlled, so it is attacker-influenced by construction and no
amount of validation changes that: you cannot escape natural language.

What can be done is refuse to let it *pretend* to be anything else.
`AgentText` holds it, and its ``__str__`` deliberately does not return it. An
f-string that interpolates one by accident — into a log line, a metric label, a
trace attribute, a system prompt for the next call — produces
``<agent text, 412 chars>`` rather than the payload. Reading the content
requires saying ``.untrusted``, which is a word a reviewer notices.

That is containment rather than sanitisation, and the distinction is the honest
one: the text still reaches a person, and a person can still be persuaded by it.
The control for that is in the console, which must render it as something the
agent said rather than as something the system knows.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

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
    ways this text leaks are all accidents of convenience — an f-string in a log
    line, a metric attribute, a trace span, the prompt for the next call — and
    every one of them goes through ``str()``. Making that path produce a
    description instead of the payload turns a silent leak into a visibly empty
    one.
    """

    untrusted: str

    def __str__(self) -> str:
        return f"<agent text, {len(self.untrusted)} chars>"

    def __repr__(self) -> str:
        return self.__str__()


@dataclass(frozen=True, slots=True)
class EscalationPacket:
    """A request for a named person's attention. The agent's only output.

    Not a decision. Nothing downstream may treat this as an approval, and
    `test_agent_boundary.py` asserts the escalation path writes no
    `approval_decision` row.
    """

    match_result_id: uuid.UUID
    owner: OwnerRole
    codes: tuple[ExceptionCode, ...]
    summary: AgentText

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


def build(
    *,
    match_result_id: uuid.UUID,
    owner: str,
    codes: tuple[str, ...],
    summary: str,
) -> EscalationPacket:
    """Turn what a model actually returned into a packet, or refuse.

    Takes strings because that is what comes back from a model: the enums are
    this side of the boundary, never the other. Coercion happens exactly here,
    fails loudly, and is the only way a packet gets made.
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
    )
