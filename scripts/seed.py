"""Seed realistic reference data.

Idempotent: it upserts on ``vendor_number`` so ``docker compose up`` twice is
safe, and it can be re-run against a warm database without duplicating rows.

The vendors here are deliberately not "Acme Corp" with a round dollar amount.
Data realism is the fastest signal of seriousness in a domain project, so these
carry the shapes a real AP system trips over: a vendor whose legal name differs
from the name printed on its invoices, non-USD currencies, a vendor on hold, and
one that has changed bank details — the single highest-severity exception this
system knows about, and the shape most real AP fraud takes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from threshold.adapters.db import dispose_engine, init_engine, session_scope
from threshold.config.settings import get_settings
from threshold.domain.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
    VendorBankAccount,
)
from threshold.observability.logging import configure_logging

log = structlog.get_logger(__name__)

#: Seed timestamps anchor here rather than to ``now()``.
#:
#: The unique constraint on a bank-account version includes ``effective_from``,
#: so deriving that column from the wall clock makes every run insert a new row
#: — the seed silently stops being idempotent and the table grows on each
#: ``docker compose up``. The smoke test caught exactly that. Anchoring to a
#: fixed instant keeps the data internally consistent (the bank change is still
#: 40 days before the newest activity) while making re-runs produce byte-
#: identical rows.
SEED_EPOCH = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)

# Local-only, and the only HMAC in this repository. A per-deployment secret in
# anything real: this fingerprints bank details so the seed can carry a vendor's
# payment history without storing the digits twice.
#
# It is deliberately *not* the alias scheme M9 once promised. That scheme was
# abandoned (D-036), and this is a different job: fingerprinting a value nobody
# is meant to recover, rather than pseudonymising one somebody is meant to read.
_SEED_HMAC_KEY = b"threshold-local-seed-key"


def _fingerprint(routing: str, account: str) -> str:
    """Comparable, not reversible.

    Storing a fingerprint rather than the number is what lets the pipeline ask
    "did this change since we last paid them?" without ever holding the value.
    """
    return hmac.new(_SEED_HMAC_KEY, f"{routing}:{account}".encode(), hashlib.sha256).hexdigest()


VENDORS: list[dict[str, str]] = [
    {
        "vendor_number": "V-100418",
        "legal_name": "Northgate Industrial Supply, LLC",
        # Invoices from this vendor are printed "Northgate Supply" — the
        # mismatch is why vendor resolution cannot be an equality check.
        "display_name": "Northgate Supply",
        "country_code": "US",
        "default_currency": "USD",
        "status": "active",
    },
    {
        "vendor_number": "V-100592",
        "legal_name": "Beckman Fasteners & Fixings GmbH",
        "display_name": "Beckman Fasteners",
        "country_code": "DE",
        "default_currency": "EUR",
        "status": "active",
    },
    {
        "vendor_number": "V-100733",
        "legal_name": "Halcyon Facilities Services Limited",
        "display_name": "Halcyon Facilities",
        "country_code": "GB",
        "default_currency": "GBP",
        "status": "active",
    },
    {
        "vendor_number": "V-100844",
        "legal_name": "Pinewood Freight Systems Inc.",
        "display_name": "Pinewood Freight",
        "country_code": "US",
        "default_currency": "USD",
        # On hold: every invoice from this vendor must stop regardless of how
        # clean it looks. A status the rules engine has to honour in M3.
        "status": "on_hold",
    },
    {
        "vendor_number": "V-100961",
        "legal_name": "Calder & Roe Professional Services LLP",
        "display_name": "Calder and Roe",
        "country_code": "US",
        "default_currency": "USD",
        "status": "active",
    },
]

# Northgate changed banks 40 days ago. Both rows are kept, which is what makes
# "changed since the last payment" answerable rather than merely current.
BANK_HISTORY: list[dict[str, object]] = [
    {
        "vendor_number": "V-100418",
        "routing": "121000248",
        "account": "8842910073",
        "days_ago_from": 900,
        "days_ago_to": 40,
    },
    {
        "vendor_number": "V-100418",
        "routing": "026009593",
        "account": "5510338291",
        "days_ago_from": 40,
        "days_ago_to": None,
    },
    {
        "vendor_number": "V-100592",
        "routing": "37040044",
        "account": "0532013000",
        "days_ago_from": 700,
        "days_ago_to": None,
    },
    {
        "vendor_number": "V-100733",
        "routing": "203253",
        "account": "44771092",
        "days_ago_from": 500,
        "days_ago_to": None,
    },
]


# --------------------------------------------------- orders and receipts ---
#
# PO-44718 is not arbitrary. It is the purchase order number the golden invoice
# fixture prints, and its lines carry the SKUs and the agreed price that fixture
# bills against, so `make smoke` exercises a real three-way match end to end
# rather than a match against nothing. Change one and the other has to move.
#
# The other two orders exist so the exception paths are demonstrable without
# hand-crafting a database: PO-44801 is agreed below what the demo bills, and
# PO-44802 has been ordered and never received.

PURCHASE_ORDERS: list[dict[str, object]] = [
    {
        "po_number": "PO-44718",
        "vendor_number": "V-100418",
        "currency": "EUR",
        "status": "open",
        "buyer_email": "r.okafor@threshold.example",
        "days_ago": 45,
        "lines": [
            # Quantity and price match the fixture exactly, and every line is
            # fully received, so the golden invoice matches clean.
            {
                "sku": "NG-0O10l",
                "description": "Hex bolt M8x40 zinc plated, DIN 933, boxed 100",
                "quantity_ordered": "12",
                "unit_price": "18.4000",
                "received": "12",
            },
            {
                "sku": "NG-0O11l",
                "description": "Hex bolt M8x41 zinc plated, DIN 933, boxed 100",
                "quantity_ordered": "12",
                "unit_price": "18.4000",
                "received": "12",
            },
            {
                "sku": "NG-0O12l",
                "description": "Hex bolt M8x42 zinc plated, DIN 933, boxed 100",
                "quantity_ordered": "12",
                "unit_price": "18.4000",
                "received": "12",
            },
        ],
    },
    {
        # Ordered from the same vendor and in the same currency as PO-44718, so
        # billing against it produces RECEIPT_MISSING and nothing else. An
        # exception demo that also trips a vendor or currency mismatch does not
        # show which rule fired.
        "po_number": "PO-44730",
        "vendor_number": "V-100418",
        "currency": "EUR",
        "status": "open",
        "buyer_email": "r.okafor@threshold.example",
        "days_ago": 4,
        "lines": [
            {
                "sku": "NG-4420k",
                "description": "Anchor bolt M12x120 hot-dip galvanised, boxed 25",
                "quantity_ordered": "8",
                "unit_price": "31.5000",
                "received": "0",
            },
        ],
    },
    {
        "po_number": "PO-44801",
        "vendor_number": "V-100592",
        "currency": "EUR",
        "status": "open",
        "buyer_email": "r.okafor@threshold.example",
        "days_ago": 30,
        "lines": [
            # Delivered in two drops, which is the normal case and the reason
            # receipts are summed rather than compared one at a time.
            {
                "sku": "BF-2201",
                "description": "Threaded rod M10, 1m, A2 stainless",
                "quantity_ordered": "40",
                "unit_price": "7.2500",
                "received": "40",
            },
        ],
    },
    {
        "po_number": "PO-44802",
        "vendor_number": "V-100733",
        "currency": "GBP",
        "status": "open",
        "buyer_email": "s.mistry@threshold.example",
        "days_ago": 6,
        "lines": [
            # Ordered, nothing received. Billing against this is RECEIPT_MISSING
            # and belongs to receiving, not to the buyer.
            {
                "sku": "HF-0090",
                "description": "Quarterly HVAC service visit",
                "quantity_ordered": "1",
                "unit_price": "1450.0000",
                "received": "0",
            },
        ],
    },
    {
        "po_number": "PO-44655",
        "vendor_number": "V-100418",
        "currency": "EUR",
        "status": "closed",
        "buyer_email": "r.okafor@threshold.example",
        "days_ago": 120,
        "lines": [
            {
                "sku": "NG-0O10l",
                "description": "Hex bolt M8x40 zinc plated, DIN 933, boxed 100",
                "quantity_ordered": "5",
                "unit_price": "18.4000",
                "received": "5",
            },
        ],
    },
]


async def _seed_orders(session: AsyncSession, vendor_ids: dict[str, uuid.UUID]) -> None:
    """Purchase orders, their lines, and the receipts against them.

    Idempotent the same way the vendors are: ``po_number`` and
    ``receipt_number`` are unique, so a second run conflicts and does nothing
    rather than growing the table. Line rows are only inserted when the order
    was newly created, because a line has no natural key of its own beyond its
    order and number -- which is exactly what the unique constraint on
    (purchase_order_id, line_number) enforces.
    """
    for spec in PURCHASE_ORDERS:
        vendor_id = vendor_ids[str(spec["vendor_number"])]
        po_number = str(spec["po_number"])
        ordered_at = SEED_EPOCH - timedelta(days=int(spec["days_ago"]))  # type: ignore[arg-type]

        order_id = (
            await session.execute(
                insert(PurchaseOrder)
                .values(
                    po_number=po_number,
                    vendor_id=vendor_id,
                    currency=str(spec["currency"]),
                    status=str(spec["status"]),
                    buyer_email=str(spec["buyer_email"]),
                    ordered_at=ordered_at,
                )
                .on_conflict_do_nothing(index_elements=[PurchaseOrder.po_number])
                .returning(PurchaseOrder.id)
            )
        ).scalar_one_or_none()

        if order_id is None:
            continue  # already seeded

        lines = spec["lines"]
        assert isinstance(lines, list)
        for number, line in enumerate(lines, start=1):
            line_id = (
                await session.execute(
                    insert(PurchaseOrderLine)
                    .values(
                        purchase_order_id=order_id,
                        line_number=number,
                        sku=str(line["sku"]),
                        description=str(line["description"]),
                        quantity_ordered=Decimal(str(line["quantity_ordered"])),
                        unit_price=Decimal(str(line["unit_price"])),
                    )
                    .returning(PurchaseOrderLine.id)
                )
            ).scalar_one()

            received = Decimal(str(line["received"]))
            if received <= 0:
                continue

            receipt_id = (
                await session.execute(
                    insert(GoodsReceipt)
                    .values(
                        receipt_number=f"GR-{po_number}-{number}",
                        purchase_order_id=order_id,
                        received_at=ordered_at + timedelta(days=3),
                        received_by="dock@threshold.example",
                    )
                    .returning(GoodsReceipt.id)
                )
            ).scalar_one()

            await session.execute(
                insert(GoodsReceiptLine).values(
                    goods_receipt_id=receipt_id,
                    purchase_order_line_id=line_id,
                    quantity_received=received,
                )
            )


async def seed() -> None:
    settings = get_settings()
    configure_logging(settings)
    init_engine(settings)

    try:
        async with session_scope() as session:
            for row in VENDORS:
                stmt = (
                    insert(Vendor)
                    .values(**row)
                    .on_conflict_do_update(
                        index_elements=[Vendor.vendor_number],
                        set_={
                            "legal_name": row["legal_name"],
                            "display_name": row["display_name"],
                            "status": row["status"],
                        },
                    )
                )
                await session.execute(stmt)

            result = await session.execute(select(Vendor.vendor_number, Vendor.id))
            vendor_ids = dict(result.all())

            now = SEED_EPOCH
            for entry in BANK_HISTORY:
                vendor_id = vendor_ids[str(entry["vendor_number"])]
                routing = str(entry["routing"])
                account = str(entry["account"])
                days_from = int(entry["days_ago_from"])  # type: ignore[arg-type]
                days_to = entry["days_ago_to"]

                stmt_bank = (
                    insert(VendorBankAccount)
                    .values(
                        vendor_id=vendor_id,
                        account_number_last4=account[-4:],
                        routing_number_last4=routing[-4:],
                        account_fingerprint=_fingerprint(routing, account),
                        effective_from=now - timedelta(days=days_from),
                        effective_to=(
                            None if days_to is None else now - timedelta(days=int(days_to))
                        ),
                    )
                    .on_conflict_do_nothing(constraint="uq_vendor_bank_account_version")
                )
                await session.execute(stmt_bank)

            await _seed_orders(session, vendor_ids)

        log.info(
            "seed.complete",
            vendors=len(VENDORS),
            bank_account_versions=len(BANK_HISTORY),
            purchase_orders=len(PURCHASE_ORDERS),
        )
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
