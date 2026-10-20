"""What the deployed pipeline did, read from inside the VPC.

    python -m scripts.aws_verify --document-ids <uuid,uuid,...> --expect 60
    python -m scripts.aws_verify --since 2026-10-20T18:00:00+00:00

Runs as a one-off ECS task on the migrate task definition (owner credentials,
private subnet), because RDS is reachable from nowhere else and the API refuses
authenticated reads when no identity provider is configured. It waits until the
named documents have a match decision, or until the timeout, then prints one
line prefixed ``AWS_VERIFY_RESULT`` that scripts/deploy_aws.py lifts out of
CloudWatch Logs. Everything it reports is a count or a timestamp difference; it
reads no document and prints no secret.

Scoping is by document id when the caller has them, which is what
scripts/load_test.py does too, so neither clock skew between the laptop and RDS
nor residue from an earlier run can move the numbers. ``--since`` is the
fallback for the cluster-wide figures (jobs, dead letters, the trial balance)
and for callers that have no ids.

The settle query is the one scripts/load_test.py uses, so a number from here and
a number from the laptop measure the same interval: ``document.received`` to
``match.decided``, both taken from the audit trail.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from threshold.config.settings import get_settings

MARKER = "AWS_VERIFY_RESULT"

SETTLE_BY_ID = text(
    """
    SELECT extract(epoch FROM (decided.occurred_at - received.occurred_at)) AS seconds
      FROM audit_event received
      JOIN match_result m ON m.document_id = received.entity_id
      JOIN audit_event decided
        ON decided.entity_id = m.id AND decided.action = 'match.decided'
     WHERE received.action = 'document.received'
       AND received.entity_id IN :ids
    """
).bindparams(bindparam("ids", expanding=True))

SETTLE_SINCE = text(
    """
    SELECT extract(epoch FROM (decided.occurred_at - received.occurred_at)) AS seconds
      FROM audit_event received
      JOIN match_result m ON m.document_id = received.entity_id
      JOIN audit_event decided
        ON decided.entity_id = m.id AND decided.action = 'match.decided'
     WHERE received.action = 'document.received'
       AND received.occurred_at >= :since
    """
)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank, the same definition load_test.py uses."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


def summarise(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min_ms": round(min(values) * 1000, 1),
        "p50_ms": round(percentile(values, 0.50) * 1000, 1),
        "p95_ms": round(percentile(values, 0.95) * 1000, 1),
        "p99_ms": round(percentile(values, 0.99) * 1000, 1),
        "max_ms": round(max(values) * 1000, 1),
        "mean_ms": round(statistics.fmean(values) * 1000, 1),
    }


class Scope:
    """Either a set of document ids or an instant; every query asks it for its WHERE."""

    def __init__(self, ids: list[uuid.UUID], since: datetime) -> None:
        self.ids = ids
        self.since = since

    def where(self, column: str, time_column: str) -> tuple[str, dict[str, Any]]:
        if self.ids:
            return f"{column} IN :ids", {"ids": self.ids}
        return f"{time_column} >= :since", {"since": self.since}

    def statement(self, sql: str) -> Any:
        stmt = text(sql)
        return stmt.bindparams(bindparam("ids", expanding=True)) if self.ids else stmt


async def count(conn: AsyncConnection, scope: Scope, sql: str, params: dict[str, Any]) -> int:
    return int((await conn.execute(scope.statement(sql), params)).scalar_one())


async def counts_by(
    conn: AsyncConnection, scope: Scope, sql: str, params: dict[str, Any]
) -> dict[str, int]:
    rows = (await conn.execute(scope.statement(sql), params)).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def scalar(conn: AsyncConnection, sql: str) -> Any:
    return (await conn.execute(text(sql))).scalar_one()


async def matched(conn: AsyncConnection, scope: Scope) -> int:
    where, params = scope.where("document_id", "decided_at")
    return await count(conn, scope, f"SELECT count(*) FROM match_result WHERE {where}", params)


async def report(conn: AsyncConnection, scope: Scope) -> dict[str, Any]:
    if scope.ids:
        settle_rows = (await conn.execute(SETTLE_BY_ID, {"ids": scope.ids})).all()
    else:
        settle_rows = (await conn.execute(SETTLE_SINCE, {"since": scope.since})).all()
    settle = [float(r[0]) for r in settle_rows if r[0] is not None]

    balance = (
        await conn.execute(
            text(
                "SELECT account, sum(amount_minor) FROM ledger_entry "
                "GROUP BY account ORDER BY account"
            )
        )
    ).all()
    roles = (
        await conn.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname IN ('threshold', 'threshold_api', "
                "'threshold_worker', 'threshold_app') "
                "ORDER BY rolname"
            )
        )
    ).all()

    received_where, received_params = scope.where("entity_id", "occurred_at")
    extracted_where, extracted_params = scope.where("document_id", "created_at")
    match_where, match_params = scope.where("document_id", "decided_at")
    decision_where, decision_params = scope.where("document_id", "decided_at")
    payment_where, payment_params = scope.where("document_id", "created_at")

    return {
        "scope": {
            "document_ids": len(scope.ids),
            "since": scope.since.isoformat(),
        },
        "received": await count(
            conn,
            scope,
            "SELECT count(*) FROM audit_event "
            f"WHERE action = 'document.received' AND {received_where}",
            received_params,
        ),
        "extracted": await count(
            conn,
            scope,
            f"SELECT count(*) FROM extraction WHERE {extracted_where}",
            extracted_params,
        ),
        "matched": await matched(conn, scope),
        "dispositions": await counts_by(
            conn,
            scope,
            f"SELECT disposition, count(*) FROM match_result WHERE {match_where} GROUP BY 1",
            match_params,
        ),
        "decisions": await counts_by(
            conn,
            scope,
            "SELECT actor_type || ':' || decision, count(*) FROM approval_decision "
            f"WHERE {decision_where} GROUP BY 1",
            decision_params,
        ),
        "payments": await counts_by(
            conn,
            scope,
            f"SELECT state, count(*) FROM payment WHERE {payment_where} GROUP BY 1",
            payment_params,
        ),
        "jobs": {
            str(r[0]): int(r[1])
            for r in (await conn.execute(text("SELECT state, count(*) FROM job GROUP BY 1"))).all()
        },
        "dead_letters": {
            "total": int(await scalar(conn, "SELECT count(*) FROM dead_letter")),
            "unreplayed": int(
                await scalar(conn, "SELECT count(*) FROM dead_letter WHERE replayed_at IS NULL")
            ),
        },
        "settle": summarise(settle),
        "trial_balance": {
            "accounts": {str(account): int(minor) for account, minor in balance},
            "sum_minor": int(sum(int(minor) for _, minor in balance)),
            "balanced": sum(int(minor) for _, minor in balance) == 0,
        },
        "roles": [
            {"name": str(name), "superuser": bool(su), "bypassrls": bool(brls)}
            for name, su, brls in roles
        ],
        "postgres": str(await scalar(conn, "SELECT version()")),
        "schema_revision": str(await scalar(conn, "SELECT version_num FROM alembic_version")),
        "workers_seen": int(
            await scalar(
                conn, "SELECT count(DISTINCT claimed_by) FROM job WHERE claimed_by IS NOT NULL"
            )
        ),
    }


def parse_ids(raw: str | None) -> list[uuid.UUID]:
    if not raw:
        return []
    return [uuid.UUID(part) for part in raw.split(",") if part.strip()]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--document-ids", default=None, help="comma-separated document ids this run submitted"
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 instant; the window when no ids are given (default: one hour ago)",
    )
    parser.add_argument(
        "--expect", type=int, default=0, help="match decisions to wait for before reporting"
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for --expect")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()

    ids = parse_ids(args.document_ids)
    since = (
        datetime.fromisoformat(args.since) if args.since else datetime.now(UTC) - timedelta(hours=1)
    )
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    scope = Scope(ids, since)

    engine = create_async_engine(str(get_settings().database_url), pool_pre_ping=True)
    started = time.monotonic()
    try:
        async with engine.connect() as conn:
            while args.expect:
                done = await matched(conn, scope)
                waited = time.monotonic() - started
                if done >= args.expect or waited > args.timeout:
                    sys.stdout.write(f"{done}/{args.expect} matched after {waited:.0f}s\n")
                    break
                sys.stdout.write(f"{done}/{args.expect} matched ({waited:.0f}s)\n")
                sys.stdout.flush()
                await asyncio.sleep(args.interval)
            result = await report(conn, scope)
    finally:
        await engine.dispose()

    result["waited_seconds"] = round(time.monotonic() - started, 1)
    sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
    sys.stdout.write(f"{MARKER} {json.dumps(result, default=str)}\n")
    sys.stdout.flush()
    return 0 if (not args.expect or result["matched"] >= args.expect) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
