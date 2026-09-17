"""Who may decide, and what deciding means.

Pure functions over values already stamped on the rows. There is no permission
matrix and no configured policy: an exception knows its owner because the
taxonomy assigned one at the moment it was raised, and that is the whole input.

**The rule: you may decide an invoice only if you own every open exception on
it.** Not "the owner of the worst exception", which is the version that looks
equivalent and is not. Exceptions sort by severity and then by code, so among
BLOCK-severity codes the alphabet decides the tie — and `arithmetic_mismatch`
(ap_clerk) sorts before `bank_details_changed` (treasury). An invoice with an
altered total *and* redirected remittance is the most likely composite fraud in
this domain, and a worst-owner rule hands it to a clerk.

The consequence is deliberate: an invoice whose exceptions have two different
owners cannot be approved by one person in M4. There is no multi-party approval
yet, so it stays blocked and the response says which roles it needs. That is a
real limitation, and it is the safe direction — the alternative is a rule that
silently picks one of two owners and lets them clear the other's finding.

`controller` and `auditor` own no exception class, so neither can approve
anything. For the auditor that is the point. For the controller it means
oversight is a seat at the table rather than a way to override the table, which
is what oversight is supposed to be.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from threshold.domain.roles import READ_ONLY_ROLES, Role
from threshold.domain.taxonomy import ExceptionCode, OwnerRole, Severity


class Decision(StrEnum):
    """What was decided, and by whom in kind."""

    #: Written by the system when a match raised nothing. The machine's decision
    #: lives in the same table as everyone else's, which is what makes "what did
    #: this system decide without asking a person" a `WHERE` rather than an
    #: absence.
    AUTO_APPROVED = "auto_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    #: Parked, pending something outside this system — a call to the vendor, a
    #: corrected purchase order. Deliberately not terminal: a hold is a state a
    #: human is in the middle of, not an outcome.
    HELD = "held"


#: Decisions after which no further decision may be recorded.
#:
#: `AUTO_APPROVED` is deliberately absent. Making it terminal would mean an
#: invoice the machine cleared can never be stopped by a person — and D-016
#: records that an invoice whose payment block cannot be read auto-approves with
#: a recorded skip. Freezing that would harden the one gap this project already
#: documents as its worst.
TERMINAL: frozenset[Decision] = frozenset({Decision.APPROVED, Decision.REJECTED})

#: The decisions that release money. `held` and `rejected` do not, and
#: `auto_approved` does -- which is the point of it being in the same table:
#: "paid without a person looking" is a WHERE clause rather than a separate path.
#:
#: Read at both ends of the payment queue: by the approval service deciding
#: whether to enqueue, and by the executor deciding whether to pay. It lived in
#: the approval service until the second reader turned out not to exist.
PAYING: frozenset[Decision] = frozenset({Decision.AUTO_APPROVED, Decision.APPROVED})


@dataclass(frozen=True, slots=True)
class OpenException:
    """The part of a `match_exception` this decision depends on."""

    code: ExceptionCode
    severity: Severity
    owner: OwnerRole


@dataclass(frozen=True, slots=True)
class Authorisation:
    """Whether this principal may decide, and if not, what is missing."""

    permitted: bool
    #: Roles that own an exception this principal does not. Empty when permitted.
    required_roles: frozenset[Role]
    #: Codes this principal would be clearing. Recorded on the decision so an
    #: approval says what it overrode rather than merely that it happened.
    overrides: tuple[ExceptionCode, ...]
    reason: str


def authorise(
    *,
    holder: frozenset[Role],
    exceptions: Iterable[OpenException],
    decision: Decision,
) -> Authorisation:
    """May a principal holding ``holder`` record ``decision`` on these exceptions?"""
    open_exceptions = tuple(exceptions)
    owners = frozenset(Role(e.owner.value) for e in open_exceptions)
    overrides = tuple(sorted({e.code for e in open_exceptions}, key=lambda c: c.value))

    if decision is Decision.AUTO_APPROVED:
        return Authorisation(
            permitted=False,
            required_roles=frozenset(),
            overrides=(),
            reason="an automatic approval is written by the system, never requested",
        )

    if holder & READ_ONLY_ROLES:
        # Checked here rather than implied by an absent grant, so a principal
        # that somehow holds both auditor and an owning role is still refused.
        return Authorisation(
            permitted=False,
            required_roles=frozenset(),
            overrides=(),
            reason="a read-only role may not decide anything",
        )

    if not open_exceptions:
        # Nothing was raised, so there is nothing to own and nothing to clear.
        # The system has already recorded its own decision; a human recording
        # another is redundant rather than wrong.
        return Authorisation(
            permitted=False,
            required_roles=frozenset(),
            overrides=(),
            reason="this invoice raised no exceptions and needs no decision",
        )

    missing = owners - holder
    if missing:
        return Authorisation(
            permitted=False,
            required_roles=missing,
            overrides=overrides,
            reason=(
                "this invoice raised exceptions owned by "
                + ", ".join(sorted(role.value for role in missing))
                + "; it cannot be decided by someone who does not own all of them"
            ),
        )

    return Authorisation(
        permitted=True,
        required_roles=frozenset(),
        overrides=overrides,
        reason="every open exception on this invoice is owned by this principal",
    )


def requires_reason(decision: Decision) -> bool:
    """Every human decision does.

    Approving over an exception without saying why produces a row that records
    that somebody clicked, which is not the same as a record of why an invoice
    was paid. The reason is the only part of the row a future reader cannot
    reconstruct.
    """
    return decision in {Decision.APPROVED, Decision.REJECTED, Decision.HELD}
