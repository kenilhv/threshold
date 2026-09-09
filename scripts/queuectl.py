"""Queue operations.

The tool an on-call engineer reaches for at 3am, which is why every command is
read-only except ``replay`` and ``reap``, and both of those say exactly what
they did.

    python -m scripts.queuectl stats
    python -m scripts.queuectl dlq
    python -m scripts.queuectl replay <dead-letter-id>
    python -m scripts.queuectl replay --all
    python -m scripts.queuectl reap
    python -m scripts.queuectl purge --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import timedelta

from sqlalchemy import select

from threshold.adapters.db import dispose_engine, init_engine, session_scope
from threshold.adapters.queue import PostgresJobQueue
from threshold.config.settings import get_settings
from threshold.domain.models import DeadLetter


async def cmd_stats() -> int:
    queue = PostgresJobQueue()
    async with session_scope() as session:
        counts = await queue.stats(session)

    width = max(len(k) for k in counts) if counts else 10
    print("\nqueue depth")
    print("-" * (width + 12))
    for key in sorted(counts):
        print(f"  {key:<{width}}  {counts[key]:>8,}")

    # The two numbers worth paging on. Everything else is context.
    if counts.get("claimable_now", 0) > 500:
        print("\n  WARNING: backlog is deep; is the worker pool running?")
    if counts.get("dead_letters_unreplayed", 0):
        print(f"\n  {counts['dead_letters_unreplayed']} dead letter(s) need a human.")
    print()
    return 0


async def cmd_dlq(limit: int) -> int:
    async with session_scope() as session:
        letters = (
            (
                await session.execute(
                    select(DeadLetter)
                    .where(DeadLetter.replayed_at.is_(None))
                    .order_by(DeadLetter.died_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    if not letters:
        print("\nnothing in the dead-letter queue.\n")
        return 0

    print(f"\n{len(letters)} unreplayed dead letter(s)\n")
    for letter in letters:
        print(f"  {letter.id}")
        print(f"    kind      {letter.kind}")
        print(f"    failure   {letter.failure_kind} after {letter.attempts} attempt(s)")
        print(f"    died      {letter.died_at.isoformat(timespec='seconds')}")
        print(f"    payload   {letter.payload}")
        print(f"    error     {letter.final_error[:200]}")
        print()
    print("replay one with:  python -m scripts.queuectl replay <id>\n")
    return 0


async def cmd_replay(dead_letter_id: str | None, replay_all: bool) -> int:
    queue = PostgresJobQueue()

    async with session_scope() as session:
        if replay_all:
            ids = list(
                (
                    await session.execute(
                        select(DeadLetter.id).where(DeadLetter.replayed_at.is_(None))
                    )
                )
                .scalars()
                .all()
            )
        else:
            if dead_letter_id is None:
                print("give a dead-letter id, or --all", file=sys.stderr)
                return 2
            try:
                ids = [uuid.UUID(dead_letter_id)]
            except ValueError:
                print(f"not a uuid: {dead_letter_id}", file=sys.stderr)
                return 2

        replayed = 0
        for letter_id in ids:
            job_id = await queue.replay_dead_letter(session, letter_id)
            if job_id is None:
                print(f"  skipped {letter_id} (already replayed, or not found)")
                continue
            print(f"  replayed {letter_id} -> job {job_id}")
            replayed += 1

    print(f"\n{replayed} job(s) back on the queue.\n")
    return 0


async def cmd_reap() -> int:
    """Manually run what the worker does every thirty seconds.

    Useful when every worker died at once and there is nothing left running to
    notice the abandoned leases.
    """
    queue = PostgresJobQueue()
    async with session_scope() as session:
        count = await queue.reap_expired_leases(session)
    print(f"\nreturned {count} abandoned job(s) to the queue.\n")
    return 0


async def cmd_purge(days: int) -> int:
    queue = PostgresJobQueue()
    async with session_scope() as session:
        deleted = await queue.purge_finished(session, older_than=timedelta(days=days))
    print(f"\ndeleted {deleted} succeeded job(s) older than {days} day(s).")
    print("dead jobs are never purged — they are the ones somebody still has to look at.\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="queuectl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="queue depth by state")

    dlq = sub.add_parser("dlq", help="list unreplayed dead letters")
    dlq.add_argument("--limit", type=int, default=20)

    replay = sub.add_parser("replay", help="put a dead letter back on the queue")
    replay.add_argument("dead_letter_id", nargs="?")
    replay.add_argument("--all", action="store_true", dest="replay_all")

    sub.add_parser("reap", help="return jobs whose worker died")

    purge = sub.add_parser("purge", help="delete old succeeded jobs")
    purge.add_argument("--days", type=int, default=7)

    return parser


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    init_engine(get_settings())
    try:
        match args.command:
            case "stats":
                return await cmd_stats()
            case "dlq":
                return await cmd_dlq(args.limit)
            case "replay":
                return await cmd_replay(args.dead_letter_id, args.replay_all)
            case "reap":
                return await cmd_reap()
            case "purge":
                return await cmd_purge(args.days)
            case _:  # pragma: no cover - argparse rejects this first
                return 2
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
