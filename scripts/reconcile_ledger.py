"""Find ledger transactions that cite nothing, and remove one deliberately.

Migration 0012 adds `ck_ledger_transaction_cites_something`: every transaction
that is not an opening balance names the payment or the match result it moves
money for. It refuses to run if existing rows violate that, rather than deleting
them or adding the constraint `NOT VALID`.

That refusal is the point. A ledger transaction with no subject is not a
formatting problem to be tidied away inside a migration -- it is money moved for
a reason nobody recorded, and a migration is the worst possible place to decide
what to do about it. This script is the better place: it shows the rows, one at
a time, with their postings, and removes one only when told which.

## Why removal and not a reversing entry

Everywhere else this system corrects a ledger by appending: a failed payment
posts a reversal rather than deleting the submission, because the ledger is an
audit trail and not a current-state table. That is the right instinct and it
does not work here. A reversing entry would itself have no subject, so it would
violate the same constraint -- there is no honest way to append your way out of
a row that refers to nothing.

So a subject-less transaction is either a real movement whose subject can be
recovered, in which case fix the row to cite it, or it is not a movement at all
and has no business in the books. This removes the second kind, names what it
removed, and prints the trial balance either side so the effect is visible.

    python scripts/reconcile_ledger.py                       # report only
    python scripts/reconcile_ledger.py --remove <uuid>       # one row, deliberately
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from urllib.parse import urlsplit

import asyncpg

#: The owner, not the runtime roles: neither of those may delete a ledger row,
#: which is exactly the property that makes this a separate, deliberate script.
DEFAULT_URL = "postgresql://threshold:threshold@localhost:5432/threshold"

ORPHANS = """
    SELECT t.id, t.kind, t.currency, t.created_at,
           (SELECT string_agg(e.account || ' ' || e.amount_minor, ', ' ORDER BY e.account)
              FROM ledger_entry e WHERE e.transaction_id = t.id) AS postings
      FROM ledger_transaction t
     WHERE t.kind <> 'opening' AND num_nonnulls(t.payment_id, t.match_result_id) <> 1
     ORDER BY t.created_at
"""


async def _trial_balance(connection: asyncpg.Connection) -> list[asyncpg.Record]:
    return await connection.fetch(
        "SELECT account, sum(amount_minor) AS total FROM ledger_entry GROUP BY account "
        "ORDER BY account"
    )


def _show(label: str, balance: list[asyncpg.Record]) -> None:
    print(f"\n  {label}")
    for row in balance:
        print(f"    {row['account']:<18} {row['total']:>14,}")
    print(f"    {'sum':<18} {sum(row['total'] for row in balance):>14,}  (must be zero)")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", metavar="UUID", help="the one transaction to remove")
    parser.add_argument(
        "--url",
        default=os.environ.get("THRESHOLD_OWNER_DATABASE_URL", DEFAULT_URL),
        help="as the schema owner; the runtime roles cannot delete a ledger row",
    )
    arguments = parser.parse_args(argv)

    connection = await asyncpg.connect(arguments.url.replace("+asyncpg", ""))
    try:
        orphans = await connection.fetch(ORPHANS)
        database = urlsplit(arguments.url).path.lstrip("/")

        if not orphans:
            print(f"every ledger transaction in {database} cites what it moves.")
            return 0

        print(f"{len(orphans)} ledger transaction(s) in {database} cite nothing:\n")
        for row in orphans:
            print(f"  {row['id']}  {row['kind']:<17} {row['created_at']:%Y-%m-%d %H:%M}")
            print(f"      {row['currency']}  {row['postings']}")

        if not arguments.remove:
            print(
                "\nNothing was changed. Each of these is a movement of money with no subject.\n"
                "If one is a real movement, make it cite its payment or match result.\n"
                "If one is not a movement at all -- test residue, a reproduction of a bug --\n"
                "remove it by id:\n\n"
                f"    python scripts/reconcile_ledger.py --remove {orphans[0]['id']}\n"
            )
            return 1

        try:
            target = uuid.UUID(arguments.remove)
        except ValueError:
            print(f"\n{arguments.remove!r} is not a uuid", file=sys.stderr)
            return 2

        if target not in {row["id"] for row in orphans}:
            print(
                f"\n{target} is not one of the transactions above. This removes only "
                "transactions that cite nothing; anything else is a real record.",
                file=sys.stderr,
            )
            return 2

        before = await _trial_balance(connection)
        async with connection.transaction():
            entries = await connection.execute(
                "DELETE FROM ledger_entry WHERE transaction_id = $1", target
            )
            header = await connection.execute(
                "DELETE FROM ledger_transaction WHERE id = $1", target
            )
        after = await _trial_balance(connection)

        print(f"\nremoved {target}: {header.lower()}, {entries.lower()}")
        _show("trial balance before", before)
        _show("trial balance after", after)
        if sum(row["total"] for row in after) != 0:
            print("\nthe books no longer balance. That is a bug in this script.", file=sys.stderr)
            return 3
        return 0
    finally:
        await connection.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
