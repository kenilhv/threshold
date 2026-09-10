"""Who may decide, as a table.

The case that matters most is the last one: an invoice carrying both an altered
total and redirected remittance details. It is the likeliest composite fraud in
this domain, and it is exactly the case a "the owner of the worst exception"
rule gets wrong.
"""

from __future__ import annotations

import pytest

from threshold.domain.approval import (
    TERMINAL,
    Decision,
    OpenException,
    authorise,
    requires_reason,
)
from threshold.domain.roles import Role
from threshold.domain.taxonomy import TAXONOMY, ExceptionCode, spec_for


def opened(*codes: ExceptionCode) -> list[OpenException]:
    return [
        OpenException(code=code, severity=spec_for(code).severity, owner=spec_for(code).owner)
        for code in codes
    ]


class TestOwnership:
    def test_the_owner_may_decide(self) -> None:
        result = authorise(
            holder=frozenset({Role.BUYER}),
            exceptions=opened(ExceptionCode.PRICE_VARIANCE),
            decision=Decision.APPROVED,
        )
        assert result.permitted
        assert result.overrides == (ExceptionCode.PRICE_VARIANCE,)

    def test_somebody_else_may_not(self) -> None:
        result = authorise(
            holder=frozenset({Role.AP_CLERK}),
            exceptions=opened(ExceptionCode.PRICE_VARIANCE),
            decision=Decision.APPROVED,
        )
        assert not result.permitted
        assert result.required_roles == frozenset({Role.BUYER})
        assert "buyer" in result.reason

    def test_holding_several_roles_is_enough_if_they_cover_everything(self) -> None:
        result = authorise(
            holder=frozenset({Role.BUYER, Role.TREASURY}),
            exceptions=opened(ExceptionCode.PRICE_VARIANCE, ExceptionCode.BANK_DETAILS_CHANGED),
            decision=Decision.APPROVED,
        )
        assert result.permitted

    @pytest.mark.parametrize("code", list(ExceptionCode))
    def test_every_code_is_decidable_by_its_own_owner(self, code: ExceptionCode) -> None:
        """No exception may be raised that nobody in the realm can act on."""
        owner = Role(spec_for(code).owner.value)
        assert authorise(
            holder=frozenset({owner}), exceptions=opened(code), decision=Decision.APPROVED
        ).permitted


class TestTheCompositeFraudCase:
    """The reason the rule is "owns everything" and not "owns the worst".

    Exceptions sort by severity then by code string. `arithmetic_mismatch` and
    `bank_details_changed` are both BLOCK, so the alphabet breaks the tie and
    the arithmetic one — owned by a clerk — sorts first. A worst-owner rule
    therefore hands an invoice with an altered total AND redirected payment
    details to a clerk, who can clear the treasury finding without treasury
    ever seeing it.
    """

    COMPOSITE = (ExceptionCode.ARITHMETIC_MISMATCH, ExceptionCode.BANK_DETAILS_CHANGED)

    def test_the_tie_break_that_makes_worst_owner_wrong_really_exists(self) -> None:
        """Guards the premise, so this test cannot quietly stop testing anything."""
        first, second = sorted(self.COMPOSITE, key=lambda c: c.value)
        assert spec_for(first).severity is spec_for(second).severity
        assert first is ExceptionCode.ARITHMETIC_MISMATCH
        assert spec_for(first).owner.value == "ap_clerk"
        assert spec_for(second).owner.value == "treasury"

    def test_a_clerk_cannot_clear_a_treasury_finding(self) -> None:
        result = authorise(
            holder=frozenset({Role.AP_CLERK}),
            exceptions=opened(*self.COMPOSITE),
            decision=Decision.APPROVED,
        )
        assert not result.permitted
        assert result.required_roles == frozenset({Role.TREASURY})

    def test_treasury_alone_cannot_either(self) -> None:
        """Symmetric. Neither owner may clear the other's finding."""
        result = authorise(
            holder=frozenset({Role.TREASURY}),
            exceptions=opened(*self.COMPOSITE),
            decision=Decision.APPROVED,
        )
        assert not result.permitted
        assert result.required_roles == frozenset({Role.AP_CLERK})

    def test_the_override_list_names_both(self) -> None:
        """An approval must say what it cleared, not merely that it happened."""
        result = authorise(
            holder=frozenset({Role.AP_CLERK, Role.TREASURY}),
            exceptions=opened(*self.COMPOSITE),
            decision=Decision.APPROVED,
        )
        assert result.permitted
        assert set(result.overrides) == set(self.COMPOSITE)


class TestRolesThatOwnNothing:
    def test_an_auditor_may_never_decide(self) -> None:
        result = authorise(
            holder=frozenset({Role.AUDITOR}),
            exceptions=opened(ExceptionCode.PRICE_VARIANCE),
            decision=Decision.APPROVED,
        )
        assert not result.permitted
        assert "read-only" in result.reason

    def test_an_auditor_who_also_holds_an_owning_role_is_still_refused(self) -> None:
        """Checked explicitly rather than implied by an absent grant."""
        result = authorise(
            holder=frozenset({Role.AUDITOR, Role.BUYER}),
            exceptions=opened(ExceptionCode.PRICE_VARIANCE),
            decision=Decision.APPROVED,
        )
        assert not result.permitted

    def test_a_controller_owns_no_exception_class(self) -> None:
        """Oversight is a seat at the table, not a way to override the table."""
        assert Role.CONTROLLER not in {Role(s.owner.value) for s in TAXONOMY.values()}
        result = authorise(
            holder=frozenset({Role.CONTROLLER}),
            exceptions=opened(ExceptionCode.PRICE_VARIANCE),
            decision=Decision.APPROVED,
        )
        assert not result.permitted


class TestDegenerateCases:
    def test_an_invoice_with_no_exceptions_needs_no_human(self) -> None:
        result = authorise(
            holder=frozenset({Role.BUYER}), exceptions=[], decision=Decision.APPROVED
        )
        assert not result.permitted
        assert "no exceptions" in result.reason

    def test_an_automatic_approval_cannot_be_requested(self) -> None:
        """It is written by the system. A person asking for one is asking to
        have their own decision recorded as the machine's."""
        result = authorise(
            holder=frozenset({Role.BUYER}),
            exceptions=opened(ExceptionCode.PRICE_VARIANCE),
            decision=Decision.AUTO_APPROVED,
        )
        assert not result.permitted

    @pytest.mark.parametrize("decision", [Decision.APPROVED, Decision.REJECTED, Decision.HELD])
    def test_every_human_decision_needs_a_reason(self, decision: Decision) -> None:
        assert requires_reason(decision)

    def test_the_machines_decision_does_not(self) -> None:
        assert not requires_reason(Decision.AUTO_APPROVED)


class TestTerminality:
    def test_only_approve_and_reject_are_terminal(self) -> None:
        assert set(TERMINAL) == {Decision.APPROVED, Decision.REJECTED}

    def test_an_automatic_approval_is_not_terminal(self) -> None:
        """D-016 records that an invoice whose payment block cannot be read
        auto-approves with a recorded skip. Freezing that would make the one
        gap this project already documents as its worst permanently
        unreachable by a human."""
        assert Decision.AUTO_APPROVED not in TERMINAL

    def test_a_hold_is_not_terminal(self) -> None:
        """A hold is a state somebody is in the middle of, not an outcome."""
        assert Decision.HELD not in TERMINAL
