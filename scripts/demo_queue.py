"""Fill the review queue with work from more than one supplier.

    python scripts/demo_queue.py          (or: make demo-queue)

`demo-match` is the argument: four invoices, four outcomes, one vendor, and the
point is that the same pipeline reaches four different answers. It is
deliberately narrow.

This is the other thing a reviewer wants, and it is a different thing: a queue
that looks like a queue. Three more suppliers, a second currency, and exception
codes the four-invoice demo never reaches. Run it after `demo-match` and the
console has seven invoices across three vendors in EUR and GBP, covering
auto-approval, review and block.

## Why this exists at all

The console is the part of this system a reviewer is least likely to run and
most likely to judge from a screenshot, and for most of this project's life the
screenshot would have been bad -- not because the console is bad, but because
the database behind it held four thousand documents cloned from one fixture
template, all reading `Northgate Supply / 899.64 / blocked`. Test residue makes
a working queue look broken.

So the demo state became a command rather than a thing somebody assembles by
hand and then cannot reproduce. `make clean && make up && make demo-match &&
make demo-queue` gets anybody to the same place.

## Every exception here is the one its invoice was built to raise

Which took three attempts, and the failures are worth recording because they
were the matcher being right:

- Changing only the PO number left the fixture's vendor block saying Northgate
  while the order belonged to Beckman, so every invoice raised
  `vendor_mismatch`. Correct, and not what the invoice was for.
- Naming the vendor but not its bank details left the fixture carrying
  Northgate's account, so every invoice raised `bank_details_changed`. Also
  correct, and also not the point.

An invoice that raises the wrong exception teaches a reader the wrong thing
about the control. The specs below name each vendor and give each one the bank
account the seed already holds for it, so the only exception raised is the one
being demonstrated.

Requires: make up, and `make demo-match` first if you want all seven.
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

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

#: The seed's own vendors, orders and current bank accounts. Duplicated here as
#: constants rather than read back from the database, because a demo that
#: queries for the values it is about to submit proves only that it can read.
#: If `seeds/` changes these, this script is meant to fail visibly.
BECKMAN = {"vendor": "Beckman Fasteners", "po": "PO-44801", "iban4": "3000", "routing4": "0044"}
HALCYON = {"vendor": "Halcyon Facilities", "po": "PO-44802", "iban4": "1092", "routing4": "3253"}


def _number(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _spec(vendor: dict[str, str], *, prefix: str, item: LineItem, **money: Decimal) -> InvoiceSpec:
    """A fixture invoice that genuinely belongs to `vendor`.

    The vendor name and the bank details travel together on purpose: either one
    left at the template's default raises an exception about the mismatch
    instead of the exception the caller wanted to show.
    """
    return dataclasses.replace(
        build_spec(),
        invoice_number=_number(prefix),
        po_number=vendor["po"],
        vendor_name=vendor["vendor"],
        bank_iban_last4=vendor["iban4"],
        bank_routing_last4=vendor["routing4"],
        line_items=(item,),
        **money,
    )


def beckman_as_agreed() -> InvoiceSpec:
    """40 rods at the agreed 7.25. Clean, and should auto-approve."""
    return _spec(
        BECKMAN,
        prefix="BF",
        item=LineItem(
            description="Threaded rod M10, 1m, A2 stainless",
            quantity=Decimal("40"),
            unit_price=Decimal("7.2500"),
            line_total=Decimal("290.0000"),
            unit_of_measure="EA",
            sku="BF-M10-1M",
        ),
        subtotal=Decimal("290.0000"),
        tax_amount=Decimal("55.1000"),
        total=Decimal("345.1000"),
    )


def beckman_over_quantity() -> InvoiceSpec:
    """52 rods against 40 ordered. `quantity_variance`, which `demo-match` never shows."""
    return _spec(
        BECKMAN,
        prefix="BF",
        item=LineItem(
            description="Threaded rod M10, 1m, A2 stainless",
            quantity=Decimal("52"),
            unit_price=Decimal("7.2500"),
            line_total=Decimal("377.0000"),
            unit_of_measure="EA",
            sku="BF-M10-1M",
        ),
        subtotal=Decimal("377.0000"),
        tax_amount=Decimal("71.6300"),
        total=Decimal("448.6300"),
    )


def halcyon_service_visit() -> InvoiceSpec:
    """A GBP service invoice, so the queue is not single-currency.

    Services are billed before anything is "received", so this lands on
    receiving as a review rather than a block -- the same code `demo-match`
    reaches from a completely different direction.
    """
    return dataclasses.replace(
        _spec(
            HALCYON,
            prefix="HF",
            item=LineItem(
                description="Quarterly HVAC service visit",
                quantity=Decimal("1"),
                unit_price=Decimal("1450.0000"),
                line_total=Decimal("1450.0000"),
                unit_of_measure="EA",
                sku="HF-HVAC-Q",
            ),
            subtotal=Decimal("1450.0000"),
            tax_amount=Decimal("290.0000"),
            total=Decimal("1740.0000"),
        ),
        currency="GBP",
    )


def main() -> int:
    scenarios = (
        ("Beckman, billed as agreed", beckman_as_agreed()),
        ("Beckman, billed for more than was ordered", beckman_over_quantity()),
        ("Halcyon, a service visit in sterling", halcyon_service_visit()),
    )

    print(f"\n{BOLD}Filling the queue{RESET}\n")
    working = Path(".demo-queue")
    working.mkdir(exist_ok=True)

    try:
        with httpx.Client(timeout=60.0) as client:
            for title, spec in scenarios:
                path = working / f"{spec.invoice_number}.pdf"
                payload = render("continental_tabular", spec, path)
                response = client.post(
                    f"{API}/v1/documents",
                    files={"file": (path.name, payload, "application/pdf")},
                )
                response.raise_for_status()
                print(f"  {title}")
                print(
                    f"    {DIM}{spec.invoice_number}  {spec.po_number}  "
                    f"{spec.currency} {spec.total}{RESET}"
                )
                path.unlink(missing_ok=True)
    finally:
        for leftover in working.glob("*.pdf"):
            leftover.unlink()
        working.rmdir()

    print(f"\n{DIM}Submitted. The worker decides them within a few seconds.{RESET}")
    print(f"{DIM}Run `make demo-match` too, then open the console at localhost:3000.{RESET}\n")
    time.sleep(8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
