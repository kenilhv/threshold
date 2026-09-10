"""Who a principal can be.

Deliberately a superset of ``OwnerRole`` rather than the same enum. Every role
that owns an exception must exist here, because a person has to be able to hold
it — but not every role that a person holds owns exceptions. ``controller`` is
oversight and ``auditor`` is read-only, and neither appears in the taxonomy.

The relationship is asserted rather than assumed: ``test_role_taxonomy.py``
checks that every ``OwnerRole`` value is a ``Role`` value, that every ``Role``
is a realm role in the committed Keycloak realm, and that at least one seeded
user holds each. Those three had drifted before this file existed — ``receiving``
and ``treasury`` own exceptions in the shipped taxonomy and were not realm roles
at all, so the two roles doing the most consequential work in the system could
not be held by anybody.

The direction of that reconciliation matters and is not reversible. The realm
was changed to match the taxonomy, never the other way round: ``owner_role`` is
stamped onto every ``match_exception`` row at the moment it is raised, so
renaming an ``OwnerRole`` silently rewrites who was responsible for work already
recorded.
"""

from __future__ import annotations

from enum import StrEnum

from threshold.domain.taxonomy import OwnerRole


class Role(StrEnum):
    """A realm role. The four exception owners, plus two that own none."""

    AP_CLERK = "ap_clerk"
    BUYER = "buyer"
    RECEIVING = "receiving"
    TREASURY = "treasury"

    #: Financial oversight. Owns no exception class, and exists so oversight is
    #: a role rather than a person.
    CONTROLLER = "controller"
    #: Read-only across every projection, including the audit trail.
    AUDITOR = "auditor"


def role_for_owner(owner: OwnerRole) -> Role:
    """The realm role that owns a class of exception.

    Total by construction: ``Role`` contains every ``OwnerRole`` value and a
    unit test enforces it, so this cannot raise for any exception the matcher
    can actually produce.
    """
    return Role(owner.value)


#: Roles that may never change the state of anything, whatever else they hold.
#: Enforced at the route rather than implied by an absent grant, so a read-only
#: principal that somehow also holds an owning role is still refused.
READ_ONLY_ROLES: frozenset[Role] = frozenset({Role.AUDITOR})
