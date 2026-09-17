"""What the process running this may do to the database, as one line per question.

`make smoke` asks this of the `api` and `worker` containers, so what it reports
is the credential those processes actually hold rather than the one the compose
file says they should. That distinction has already been wrong once: `/readyz`
answered `SELECT 1`, which needs no grant, and reported ready above a role that
could not claim a job.

It exists as a file because the first version was Python inlined into
`scripts/smoke.sh` inside a double-quoted command substitution, where `$1` in an
asyncpg parameter became the shell's first positional argument and `set -u`
killed the run. D-017 is about exactly this: the shell is not a good place to
keep a program.

Every statement runs inside a transaction that is rolled back, so nothing here
writes anything even when a refusal fails to arrive.

    identity      threshold_worker super=False bypassrls=False
    audit_update  42501
    ...

Each line is `<name> <result>`, and a result is either a SQLSTATE, `PERMITTED`,
or `SKIPPED` when the fixture a probe needs is not in this database.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg

DEFAULT_URL = os.environ.get("THRESHOLD_DATABASE_URL", "")


async def _attempt(connection: asyncpg.Connection, statement: str, *args: object) -> str:
    """The SQLSTATE this was refused with, or PERMITTED. Never leaves a row behind."""
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(statement, *args)
    except asyncpg.PostgresError as refusal:
        return str(refusal.sqlstate)
    finally:
        await transaction.rollback()
    return "PERMITTED"


DECISION = (
    "insert into approval_decision (match_result_id, document_id, decision, actor_type, "
    "actor_id, actor_label, reason, overrode) values ($1, $2, $3, $4, $5, $6, $7, '[]'::jsonb)"
)


async def main() -> int:
    url = (DEFAULT_URL or "").replace("+asyncpg", "")
    if not url:
        print("identity NO_DATABASE_URL")
        return 1

    connection = await asyncpg.connect(url)
    try:
        role = await connection.fetchrow(
            "select current_user as name, rolsuper, rolbypassrls from pg_roles "
            "where rolname = current_user"
        )
        print(f"identity {role['name']} super={role['rolsuper']} bypassrls={role['rolbypassrls']}")

        print("audit_update " + await _attempt(connection, "update audit_event set action = 'x'"))
        print(
            "disable_trigger "
            + await _attempt(connection, "alter table audit_event disable trigger all")
        )
        print(
            "payment_amount "
            + await _attempt(connection, "update payment set amount_minor = amount_minor * 5")
        )
        print(
            "write_match_result "
            + await _attempt(
                connection,
                "insert into match_result (document_id, extraction_id, version, disposition, "
                "policy_version) values (gen_random_uuid(), gen_random_uuid(), 1, "
                "'auto_approve', 'probe')",
            )
        )

        # The forgeries, on a real blocked invoice if this database has one.
        blocked = await connection.fetchrow(
            "select id, document_id from match_result where disposition = 'blocked' limit 1"
        )
        if blocked is None:
            print("human_decision SKIPPED")
            print("auto_approve_blocked SKIPPED")
        else:
            print(
                "human_decision "
                + await _attempt(
                    connection,
                    DECISION,
                    blocked["id"],
                    blocked["document_id"],
                    "approved",
                    "human",
                    f"probe-{uuid.uuid4().hex[:8]}",
                    "A Person Who Does Not Exist",
                    "looks fine to me",
                )
            )
            print(
                "auto_approve_blocked "
                + await _attempt(
                    connection,
                    DECISION,
                    blocked["id"],
                    blocked["document_id"],
                    "auto_approved",
                    "system",
                    None,
                    None,
                    None,
                )
            )
        return 0
    finally:
        await connection.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
