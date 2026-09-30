"""The console, checked end to end as the people who use it.

    python scripts/check_console.py        (or: make check-console)

Drives the running console server-side as a clerk and as a buyer, by building
the console's own session cookie from a token minted through the local
`threshold-tests` client -- the same harness the integration suite uses -- so
no password is typed into a browser. It checks:
  - the queue renders, and tells a clerk what is not theirs
  - an invoice renders the refusal for the wrong owner and the form for the right one
  - the form works WITHOUT JavaScript (progressive enhancement), end to end
  - no restricted value appears anywhere in the rendered HTML
"""

from __future__ import annotations

import base64
import dataclasses
import html
import json
import re
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.unit.test_fixture_layouts import build_spec, render

API = "http://localhost:8000"
CONSOLE = "http://localhost:3000"
TOKEN_URL = "http://localhost:8080/realms/threshold/protocol/openid-connect/token"

results: list[tuple[bool, str]] = []


def check(ok: bool, name: str) -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


def _bearer(client: httpx.Client, user: str) -> dict[str, str]:
    """An Authorization header for `user`. Reads need one since M9."""
    token, _ = mint(client, user)
    return {"Authorization": f"Bearer {token}"}


def mint(client: httpx.Client, user: str) -> tuple[str, dict[str, Any]]:
    r = client.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "threshold-tests",
            "client_secret": "local-tests-only",
            "username": user,
            "password": user,
        },
    )
    r.raise_for_status()
    body = r.json()
    payload = body["access_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return body["access_token"], claims


def session_cookie(token: str, claims: dict[str, Any]) -> str:
    session = {
        "accessToken": token,
        "username": claims.get("preferred_username", "?"),
        "roles": claims.get("realm_access", {}).get("roles", []),
        "expiresAt": int(claims["exp"]),
    }
    return base64.urlsafe_b64encode(json.dumps(session).encode()).decode().rstrip("=")


def submit_over_billed(client: httpx.Client) -> str:
    spec = build_spec()
    items = tuple(
        dataclasses.replace(i, unit_price=Decimal("21.0000"), line_total=Decimal("252.0000"))
        for i in spec.line_items
    )
    spec = dataclasses.replace(
        spec,
        invoice_number=f"CONSOLE-{uuid.uuid4().hex[:8].upper()}",
        line_items=items,
        subtotal=Decimal("756.0000"),
        tax_amount=Decimal("143.6400"),
        total=Decimal("899.6400"),
    )
    path = ROOT / f".console-{spec.invoice_number}.pdf"
    render("continental_tabular", spec, path)
    try:
        r = client.post(
            f"{API}/v1/documents",
            files={"file": (path.name, path.read_bytes(), "application/pdf")},
        )
        r.raise_for_status()
        doc: str = r.json()["document_id"]
    finally:
        path.unlink(missing_ok=True)
    for _ in range(60):
        if (
            client.get(
                f"{API}/v1/documents/{doc}/match", headers=_bearer(client, "auditor")
            ).status_code
            == 200
        ):
            return doc
        time.sleep(0.5)
    raise SystemExit("worker never matched the invoice")


def main() -> None:
    with httpx.Client(timeout=30) as client:
        doc = submit_over_billed(client)
        print(f"submitted over-billed invoice {doc}\n")

        clerk_token, clerk_claims = mint(client, "clerk")
        buyer_token, buyer_claims = mint(client, "buyer")
        clerk = {"threshold_session": session_cookie(clerk_token, clerk_claims)}
        buyer = {"threshold_session": session_cookie(buyer_token, buyer_claims)}

        print("unauthenticated")
        r = client.get(f"{CONSOLE}/queue", follow_redirects=False)
        check(
            r.status_code in (302, 303, 307) and "/api/auth/login" in r.headers.get("location", ""),
            "an anonymous request to the queue is sent to sign in",
        )

        print("\nas clerk")
        r = client.get(f"{CONSOLE}/queue", cookies=clerk)
        check(r.status_code == 200, "the queue renders")
        check("Waiting for a person" in r.text, "the queue heading is there")
        check(
            "Not yours to decide" in r.text,
            "rows the clerk does not own say so instead of offering a button",
        )
        check(
            "tile" not in r.text.lower() and "<canvas" not in r.text, "no dashboard tiles or charts"
        )

        r = client.get(f"{CONSOLE}/invoice/{doc}", cookies=clerk)
        check(r.status_code == 200, "the invoice renders")
        page = html.unescape(r.text)
        check("Billed above the agreed price" in page, "the exception is shown, in words")
        check("21.00" in page and "18.40" in page, "with the billed and agreed prices beside it")
        check("You cannot decide this invoice." in page, "the refusal is where the button would be")
        check("owned by buyer" in page, "the refusal names the owning role")
        check('name="reason"' not in r.text, "no decision form is rendered for the wrong owner")

        print("\nas buyer")
        r = client.get(f"{CONSOLE}/invoice/{doc}", cookies=buyer)
        check(r.status_code == 200, "the invoice renders")
        check('name="reason"' in r.text, "the decision form is rendered for the owner")
        check(
            "price_variance" in html.unescape(r.text),
            "the form says what it will record as cleared",
        )

        # Progressive enhancement: submit the form exactly as a browser with
        # JavaScript disabled would, using the hidden inputs Next renders.
        form_match = re.search(r'<form class="decide"[^>]*>(.*?)</form>', r.text, re.S)
        check(form_match is not None, "the decision form is present in server-rendered HTML")
        fields: dict[str, str] = {}
        if form_match:
            for m in re.finditer(r"<input([^>]*)>", form_match.group(1)):
                attrs = dict(re.findall(r'([\w$:.-]+)="([^"]*)"', m.group(1)))
                if "name" in attrs:
                    fields[html.unescape(attrs["name"])] = html.unescape(attrs.get("value", ""))
        fields["decision"] = "approved"
        fields["reason"] = (
            "Price rise to 21.00 agreed with Northgate on the July call (console no-JS path)."
        )
        check(
            any(k.startswith("$ACTION") for k in fields),
            "the form carries a server-action reference, so it works without JS",
        )

        r = client.post(
            f"{CONSOLE}/invoice/{doc}",
            cookies=buyer,
            files={k: (None, v) for k, v in fields.items()},
            follow_redirects=False,
        )
        check(
            r.status_code in (200, 303, 307),
            f"the no-JS submission is accepted (HTTP {r.status_code})",
        )

        history = client.get(
            f"{API}/v1/documents/{doc}/decisions",
            headers={"Authorization": f"Bearer {buyer_token}"},
        ).json()
        human = [d for d in history if d["decision"] == "approved"]
        check(len(human) == 1, "the API recorded exactly one approval")
        if human:
            check(
                human[0]["actor_label"] == "buyer",
                "recorded as the buyer, not as a service account",
            )
            check(human[0]["overrode"] == ["price_variance"], "recorded with what it cleared")
            check(
                "console no-JS path" in (human[0]["reason"] or ""),
                "recorded with the reason typed into the form",
            )

        r = client.get(f"{CONSOLE}/invoice/{doc}", cookies=buyer)
        page = html.unescape(r.text)
        check("There is no undo." in page, "a decided invoice says it is final")
        check('name="reason"' not in r.text, "and offers no second decision")

        print("\nrestricted values")
        pages = [
            client.get(f"{CONSOLE}/queue", cookies=clerk).text,
            client.get(f"{CONSOLE}/invoice/{doc}", cookies=clerk).text,
            client.get(f"{CONSOLE}/invoice/{doc}", cookies=buyer).text,
        ]
        leaked = [
            probe
            for probe in (
                "DE811907980",
                "5510338291",
                "026009593",
                "account_fingerprint",
                "bank_account_last4",
                "vendor_tax_id",
            )
            if any(probe in p for p in pages)
        ]
        check(not leaked, f"no restricted value in any rendered page {leaked or ''}")
        check(
            all(buyer_token not in p and clerk_token not in p for p in pages),
            "the access token never appears in HTML sent to the browser",
        )

    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
