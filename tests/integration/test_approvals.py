"""Approvals over HTTP, with real tokens and the real database.

The authorisation rule is covered exhaustively as a table in
`tests/unit/test_approval.py`. What can only be checked here is that the rule
reaches the endpoint, that the queue and the decision agree about who may act,
and that the two concurrency refusals are enforced by the database rather than
by a read.

Requires: make up
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from tests.integration.test_auth import mint
from tests.unit.test_fixture_layouts import build_spec, render

pytestmark = pytest.mark.integration

API = "http://localhost:8000"


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30) as c:
        try:
            c.get(f"{API}/healthz")
        except httpx.HTTPError:
            pytest.skip("the stack is not running; `make up`")
        yield c


def auth(client: httpx.Client, user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {mint(client, user)}"}


def submit(client: httpx.Client, *, over_billed: bool) -> str:
    """Render an invoice, submit it, and wait for a decision to exist."""
    spec = build_spec()
    if over_billed:
        items = tuple(
            dataclasses.replace(i, unit_price=Decimal("21.0000"), line_total=Decimal("252.0000"))
            for i in spec.line_items
        )
        spec = dataclasses.replace(
            spec,
            line_items=items,
            subtotal=Decimal("756.0000"),
            tax_amount=Decimal("143.6400"),
            total=Decimal("899.6400"),
        )
    spec = dataclasses.replace(spec, invoice_number=f"AP-{uuid.uuid4().hex[:10].upper()}")

    path = Path(f".approval-{spec.invoice_number}.pdf")
    try:
        render("continental_tabular", spec, path)
        response = client.post(
            f"{API}/v1/documents",
            files={"file": (path.name, path.read_bytes(), "application/pdf")},
        )
        response.raise_for_status()
        document_id: str = response.json()["document_id"]
    finally:
        path.unlink(missing_ok=True)

    for _ in range(60):
        match = client.get(
            f"{API}/v1/documents/{document_id}/match", headers=auth(client, "auditor")
        )
        if match.status_code == 200:
            return document_id
        import time

        time.sleep(0.5)
    pytest.fail(f"no decision for {document_id}; is the worker running?")


def match_of(client: httpx.Client, document_id: str) -> dict[str, object]:
    response = client.get(
        f"{API}/v1/documents/{document_id}/match", headers=auth(client, "auditor")
    )
    response.raise_for_status()
    body: dict[str, object] = response.json()
    return body


def decide(
    client: httpx.Client,
    user: str,
    match_result_id: str,
    *,
    decision: str = "approved",
    reason: str = "checked with the vendor",
    seen_version: int = 1,
) -> httpx.Response:
    return client.post(
        f"{API}/v1/match-results/{match_result_id}/decision",
        headers=auth(client, user),
        json={"decision": decision, "reason": reason, "seen_version": seen_version},
    )


class TestTheMachineIsAPeer:
    def test_a_clean_invoice_already_has_a_decision(self, client: httpx.Client) -> None:
        """The system's own decision, in the table humans write to.

        Without this, "the system cleared it" is the absence of a human row —
        indistinguishable from nobody having looked yet.
        """
        document_id = submit(client, over_billed=False)
        response = client.get(
            f"{API}/v1/documents/{document_id}/decisions", headers=auth(client, "clerk")
        )
        assert response.status_code == 200
        decisions = response.json()
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "auto_approved"
        assert decisions[0]["actor_label"] == "system"
        assert decisions[0]["reason"] is None, "the machine has no judgement to explain"

    def test_a_blocked_invoice_has_none(self, client: httpx.Client) -> None:
        document_id = submit(client, over_billed=True)
        response = client.get(
            f"{API}/v1/documents/{document_id}/decisions", headers=auth(client, "clerk")
        )
        assert response.json() == []


class TestTheQueueAndTheRuleAgree:
    def test_the_queue_needs_a_token(self, client: httpx.Client) -> None:
        assert client.get(f"{API}/v1/queue").status_code == 401

    def test_a_clerk_sees_a_buyers_invoice_and_is_told_why_they_cannot_act(
        self, client: httpx.Client
    ) -> None:
        """The missing button, with its reason on screen.

        Showing work and then refusing it is worse than showing nothing, so the
        queue computes `can_decide` with the same function the decision
        endpoint enforces.
        """
        document_id = submit(client, over_billed=True)
        queue = client.get(f"{API}/v1/queue", headers=auth(client, "clerk")).json()
        item = next(i for i in queue if i["document_id"] == document_id)

        assert item["exception_codes"] == ["price_variance"]
        assert item["owners"] == ["buyer"]
        assert item["can_decide"] is False
        assert "buyer" in item["blocked_reason"]

    def test_the_buyer_sees_the_same_invoice_as_actionable(self, client: httpx.Client) -> None:
        document_id = submit(client, over_billed=True)
        queue = client.get(
            f"{API}/v1/queue", headers=auth(client, "buyer"), params={"mine": True}
        ).json()
        assert any(i["document_id"] == document_id and i["can_decide"] for i in queue)

    def test_auto_approved_invoices_are_not_in_anybodys_queue(self, client: httpx.Client) -> None:
        """A queue containing work nobody has to do trains people to skim it."""
        document_id = submit(client, over_billed=False)
        queue = client.get(f"{API}/v1/queue", headers=auth(client, "clerk")).json()
        assert not [i for i in queue if i["document_id"] == document_id]


class TestDeciding:
    def test_the_owner_approves_and_the_row_says_what_it_cleared(
        self, client: httpx.Client
    ) -> None:
        document_id = submit(client, over_billed=True)
        match = match_of(client, document_id)
        response = decide(
            client,
            "buyer",
            str(match["match_result_id"]),
            seen_version=int(match["version"]),  # type: ignore[arg-type]
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["decision"] == "approved"
        assert body["actor_label"] == "buyer"
        assert body["overrode"] == ["price_variance"]
        assert body["reason"]

    def _queue_item(self, client: httpx.Client, user: str, document_id: str) -> dict[str, object]:
        queue = client.get(f"{API}/v1/queue", headers=auth(client, user)).json()
        item: dict[str, object] = next(i for i in queue if i["document_id"] == document_id)
        return item

    def test_the_wrong_owner_is_refused_with_the_role_it_needs(self, client: httpx.Client) -> None:
        document_id = submit(client, over_billed=True)
        item = self._queue_item(client, "clerk", document_id)
        response = decide(
            client,
            "clerk",
            str(item["match_result_id"]),
            seen_version=int(item["version"]),  # type: ignore[arg-type]
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["required_roles"] == ["buyer"]

    def test_a_read_only_role_is_refused(self, client: httpx.Client) -> None:
        document_id = submit(client, over_billed=True)
        item = self._queue_item(client, "auditor", document_id)
        response = decide(
            client,
            "auditor",
            str(item["match_result_id"]),
            seen_version=int(item["version"]),  # type: ignore[arg-type]
        )
        assert response.status_code == 403
        assert "read-only" in str(response.json()["detail"])

    def test_a_decision_without_a_reason_is_refused(self, client: httpx.Client) -> None:
        """The reason is the only part of the record nobody can reconstruct."""
        document_id = submit(client, over_billed=True)
        item = self._queue_item(client, "buyer", document_id)
        response = client.post(
            f"{API}/v1/match-results/{item['match_result_id']}/decision",
            headers=auth(client, "buyer"),
            json={"decision": "approved", "reason": "", "seen_version": item["version"]},
        )
        assert response.status_code == 422

    def test_deciding_on_evidence_that_moved_is_refused(self, client: httpx.Client) -> None:
        """Not a retry. The right next step is to look at the invoice again."""
        document_id = submit(client, over_billed=True)
        item = self._queue_item(client, "buyer", document_id)
        response = decide(client, "buyer", str(item["match_result_id"]), seen_version=999)
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["seen_version"] == 999
        assert detail["current_version"] == item["version"]

    def test_a_second_terminal_decision_is_refused(self, client: httpx.Client) -> None:
        """Enforced by a partial unique index, not by a read-then-write."""
        document_id = submit(client, over_billed=True)
        item = self._queue_item(client, "buyer", document_id)
        first = decide(
            client,
            "buyer",
            str(item["match_result_id"]),
            seen_version=int(item["version"]),  # type: ignore[arg-type]
        )
        assert first.status_code == 201

        second = decide(
            client,
            "buyer",
            str(item["match_result_id"]),
            reason="deciding again",
            seen_version=int(item["version"]),  # type: ignore[arg-type]
        )
        assert second.status_code == 409
        assert second.json()["detail"]["decision"] == "approved"

    def test_a_decided_invoice_leaves_the_queue(self, client: httpx.Client) -> None:
        document_id = submit(client, over_billed=True)
        item = self._queue_item(client, "buyer", document_id)
        assert (
            decide(
                client,
                "buyer",
                str(item["match_result_id"]),
                seen_version=int(item["version"]),  # type: ignore[arg-type]
            ).status_code
            == 201
        )
        queue = client.get(f"{API}/v1/queue", headers=auth(client, "buyer")).json()
        assert not [i for i in queue if i["document_id"] == document_id]

    def test_a_hold_is_not_terminal_and_the_invoice_stays(self, client: httpx.Client) -> None:
        """A hold is a state somebody is in the middle of, not an outcome."""
        document_id = submit(client, over_billed=True)
        item = self._queue_item(client, "buyer", document_id)
        held = decide(
            client,
            "buyer",
            str(item["match_result_id"]),
            decision="held",
            reason="waiting on the vendor to confirm the price rise",
            seen_version=int(item["version"]),  # type: ignore[arg-type]
        )
        assert held.status_code == 201

        queue = client.get(f"{API}/v1/queue", headers=auth(client, "buyer")).json()
        assert [i for i in queue if i["document_id"] == document_id], (
            "a held invoice is still somebody's problem"
        )
        approved = decide(
            client,
            "buyer",
            str(item["match_result_id"]),
            seen_version=int(item["version"]),  # type: ignore[arg-type]
        )
        assert approved.status_code == 201


class TestTheTrailIsAppendOnly:
    def test_every_decision_survives_the_next_one(self, client: httpx.Client) -> None:
        document_id = submit(client, over_billed=True)
        item = self._item(client, document_id)
        decide(
            client,
            "buyer",
            item["match_result_id"],
            decision="held",
            reason="calling the vendor",
            seen_version=item["version"],
        )
        decide(
            client,
            "buyer",
            item["match_result_id"],
            reason="vendor confirmed the increase",
            seen_version=item["version"],
        )

        history = client.get(
            f"{API}/v1/documents/{document_id}/decisions", headers=auth(client, "auditor")
        ).json()
        assert [d["decision"] for d in history] == ["held", "approved"]
        assert all(d["reason"] for d in history)

    @staticmethod
    def _item(client: httpx.Client, document_id: str) -> dict:  # type: ignore[type-arg]
        queue = client.get(f"{API}/v1/queue", headers=auth(client, "buyer")).json()
        return next(i for i in queue if i["document_id"] == document_id)


class TestTheResponseFollowsTheCommit:
    """A 201 means the row exists, now, for anyone who asks.

    Regression test for a measured defect. `get_session` commits in its exit
    code, and a `yield` dependency defaults to request scope, which runs that
    exit code after the response is sent. Approving and reading back in the same
    breath came back empty in 6 of 8 trials. The console redirects to the
    invoice straight after a decision, so it could render a just-approved invoice
    as undecided — form and all — and invite a second decision.

    The quieter failure was worse: a commit that failed after the response had
    gone left the client holding a 201 for a decision that was never recorded.
    """

    def test_a_decision_is_readable_the_moment_it_is_acknowledged(
        self, client: httpx.Client
    ) -> None:
        trials = 6
        unreadable = 0
        for _ in range(trials):
            document_id = submit(client, over_billed=True)
            match = match_of(client, document_id)
            response = decide(
                client,
                "buyer",
                str(match["match_result_id"]),
                seen_version=int(match["version"]),  # type: ignore[arg-type]
            )
            assert response.status_code == 201, response.text

            # No sleep. That is the entire point.
            history = client.get(
                f"{API}/v1/documents/{document_id}/decisions", headers=auth(client, "buyer")
            ).json()
            unreadable += not any(d["decision"] == "approved" for d in history)

        assert unreadable == 0, (
            f"{unreadable}/{trials} acknowledged decisions were not yet readable"
        )
