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
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from threshold.adapters.db import dispose_engine, init_engine, session_scope
from threshold.config.settings import get_settings
from threshold.domain.models import Vendor, VendorBankAccount
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

# Local-only. The real one is a per-deployment secret; see M9.
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

        log.info(
            "seed.complete",
            vendors=len(VENDORS),
            bank_account_versions=len(BANK_HISTORY),
        )
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
