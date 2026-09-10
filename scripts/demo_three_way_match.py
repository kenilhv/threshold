"""The M3 demo: three invoices, three different answers, and the reason for each.

    python scripts/demo_three_way_match.py

Every invoice here is a real PDF, rendered by the same fixture generator the
tests use, submitted over HTTP to the running API and judged by the worker. The
purchase orders and receipts come from the seed. Nothing is stubbed and no
result is asserted in advance -- whatever the system decides is what prints.

The point is not that it blocks things. It is that the same pipeline approves
one invoice and blocks another, says which rule fired, by how much, and whose
desk it lands on.

Requires: make up
"""

from __future__ import annotations

import dataclasses
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seeds.invoice_spec import InvoiceSpec, LineItem
from tests.unit.test_fixture_layouts import build_spec, render

API = "http://localhost:8000"

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[31m",
    "\033[32m",
    "\033[33m",
    "\033[0m",
)
COLOUR = {"auto_approve": GREEN, "needs_review": YELLOW, "blocked": RED}


def clean() -> InvoiceSpec:
    """Bills PO-44718 exactly as agreed, and every line was received."""
    return dataclasses.replace(build_spec(), invoice_number=_number("CLEAN"))


def over_billed() -> InvoiceSpec:
    """Same order, 21.00 a box against an agreed 18.40."""
    spec = build_spec()
    items = tuple(
        dataclasses.replace(item, unit_price=Decimal("21.0000"), line_total=Decimal("252.0000"))
        for item in spec.line_items
    )
    return dataclasses.replace(
        spec,
        invoice_number=_number("OVER"),
        line_items=items,
        subtotal=Decimal("756.0000"),
        tax_amount=Decimal("143.6400"),
        total=Decimal("899.6400"),
    )


def billed_before_receipt() -> InvoiceSpec:
    """Bills PO-44730, which was ordered four days ago and has not arrived."""
    spec = build_spec()
    item = LineItem(
        description="Anchor bolt M12x120 hot-dip galvanised, boxed 25",
        quantity=Decimal("8"),
        unit_price=Decimal("31.5000"),
        line_total=Decimal("252.0000"),
        unit_of_measure="BOX",
        sku="NG-4420k",
    )
    return dataclasses.replace(
        spec,
        invoice_number=_number("EARLY"),
        po_number="PO-44730",
        line_items=(item,),
        subtotal=Decimal("252.0000"),
        tax_amount=Decimal("47.8800"),
        total=Decimal("299.8800"),
    )


def _number(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def submit(client: httpx.Client, spec: InvoiceSpec, path: Path) -> str:
    render("continental_tabular", spec, path)
    response = client.post(
        f"{API}/v1/documents",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        timeout=30,
    )
    response.raise_for_status()
    return str(response.json()["document_id"])


def await_decision(client: httpx.Client, document_id: str) -> dict[str, object]:
    """Poll. Matching is asynchronous, which is the contract, not a limitation."""
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        body = client.get(f"{API}/v1/documents/{document_id}/match", timeout=15)
        if body.status_code == 200:
            return dict(body.json())
        time.sleep(0.4)
    raise SystemExit(f"no decision for {document_id} within 45s -- is the worker running?")


def main() -> None:
    scenarios = (
        ("Billed as agreed, goods received", clean()),
        ("Billed above the agreed price", over_billed()),
        ("Billed before the goods arrived", billed_before_receipt()),
    )

    print(f"\n{BOLD}Three-way match{RESET}")
    print("Each invoice is rendered, submitted over HTTP, and judged by the worker.\n")

    with httpx.Client() as client:
        submitted = []
        for title, spec in scenarios:
            path = Path(f".demo-{spec.invoice_number}.pdf")
            submitted.append((title, spec, path, submit(client, spec, path)))

        for title, spec, path, document_id in submitted:
            decision = await_decision(client, document_id)
            disposition = str(decision["disposition"])
            colour = COLOUR.get(disposition, "")

            print(f"{BOLD}{title}{RESET}")
            print(f"  invoice   {spec.invoice_number}   order {spec.po_number}")
            print(f"  decision  {colour}{disposition.replace('_', ' ').upper()}{RESET}")

            exceptions = decision.get("exceptions") or []
            if not exceptions:
                print(f"  {DIM}no exceptions -- it may pay without a human{RESET}")
            for item in exceptions:
                assert isinstance(item, dict)
                due = str(item["due_at"])[:16].replace("T", " ")
                print(
                    f"    {item['code']}  {DIM}[{item['severity']}"
                    f" -> {item['owner_role']}, due {due}]{RESET}"
                )
                print(f"      {item['detail']}")

            for skipped in decision.get("skipped_checks") or []:
                print(f"    {YELLOW}not checked{RESET}  {skipped}")

            print(f"  {DIM}policy {decision['policy_version']}{RESET}\n")
            path.unlink(missing_ok=True)

    print(f"{DIM}Same pipeline, same thresholds, three different answers.{RESET}\n")


if __name__ == "__main__":
    main()
