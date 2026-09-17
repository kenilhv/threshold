"""Submit invoices as fast as asked and measure what the pipeline actually does.

M10's deliverable is measured numbers, and the point of measuring them here
rather than quoting a benchmark is that this system's interesting latency is not
the HTTP request. `POST /v1/documents` stores bytes and enqueues; the work
happens afterwards, in a worker, through three more jobs. A load test that
reported request latency would be reporting the least interesting number this
architecture produces.

So it measures two things and keeps them apart.

**Accept latency** -- how long the API takes to take responsibility for a
document. That is the number a supplier's system experiences, and it is a
write to MinIO plus two rows.

**Settle latency** -- `document.received` to `match.decided`, read from the
audit trail after the queue has drained. That is the number a person waiting for
an invoice to appear in their queue experiences, and it includes every queue
wait, every lease, and the extractor. It is measurable at all because migration
0013 made `occurred_at` a real clock rather than the transaction's start time.

## What this is not

It is not a benchmark of the design. It runs against Docker Desktop on a laptop,
sharing a CPU with Postgres, MinIO, Keycloak, Grafana, Tempo and Prometheus.
Every number it produces is a floor. What it is good for is the *shape*: whether
accept latency stays flat as concurrency rises, whether the queue drains at a
rate that looks like the worker's concurrency rather than the submitter's, and
whether anything falls over.

It also deliberately does not tune anything. A load test whose first act is to
raise the pool size measures the tuning, not the system.

    python scripts/load_test.py                    # 60 invoices, 8 at a time
    python scripts/load_test.py --count 200 --concurrency 16
    python scripts/load_test.py --json results.json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

API = "http://localhost:8000"
OWNER_DSN = "postgresql://threshold:threshold@localhost:5432/threshold"

#: Long enough that a slow laptop is not mistaken for a stuck queue, short
#: enough that a genuinely stuck queue is reported rather than waited on.
DRAIN_TIMEOUT_SECONDS = 300.0


def percentile(values: list[float], fraction: float) -> float:
    """The value at ``fraction`` through a sorted sample, nearest-rank.

    Not `statistics.quantiles`: that interpolates, which invents a latency
    nobody observed. With sixty samples the difference is visible.
    """
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered) + 0.5) - 1))
    return ordered[index]


def summarise(name: str, values: list[float]) -> dict[str, Any]:
    return {
        "name": name,
        "n": len(values),
        "min_ms": round(min(values) * 1000, 1) if values else None,
        "p50_ms": round(percentile(values, 0.50) * 1000, 1) if values else None,
        "p95_ms": round(percentile(values, 0.95) * 1000, 1) if values else None,
        "p99_ms": round(percentile(values, 0.99) * 1000, 1) if values else None,
        "max_ms": round(max(values) * 1000, 1) if values else None,
        "mean_ms": round(statistics.fmean(values) * 1000, 1) if values else None,
    }


def render_invoice(path: Path, number: str) -> bytes:
    """One unique invoice PDF.

    The invoice number is varied rather than the seed, for the reason
    `smoke.sh` documents: the fixtures are deterministic, so a new seed alone
    can still render byte-identical output, and the system would correctly
    deduplicate every submission after the first.
    """
    from tests.unit.test_fixture_layouts import build_spec, render

    spec = dataclasses.replace(build_spec(), invoice_number=number)
    return render("continental_tabular", spec, path)


async def submit(
    client: httpx.AsyncClient, payload: bytes, gate: asyncio.Semaphore
) -> tuple[str | None, float, int]:
    """One document, its accept latency, and the status it came back with."""
    async with gate:
        started = time.perf_counter()
        try:
            response = await client.post(
                f"{API}/v1/documents",
                files={"file": ("invoice.pdf", payload, "application/pdf")},
                timeout=60.0,
            )
        except httpx.HTTPError as failed:
            print(f"  submit failed: {failed}", file=sys.stderr)
            return None, time.perf_counter() - started, 0
        elapsed = time.perf_counter() - started

    # 202 for a new document, 200 for one this system has already seen: the
    # duplicate check is a control, not a failure, and a load test that counted
    # it as one would report a broken pipeline every time it ran twice.
    if response.status_code not in (200, 202):
        return None, elapsed, response.status_code
    body = response.json()
    return body.get("document_id"), elapsed, response.status_code


async def drain(connection: asyncpg.Connection, document_ids: list[str]) -> tuple[int, float]:
    """Wait until every document has a match decision. Returns (decided, seconds)."""
    started = time.perf_counter()
    ids = [uuid.UUID(d) for d in document_ids]
    while True:
        decided = await connection.fetchval(
            "select count(*) from match_result where document_id = any($1::uuid[])", ids
        )
        elapsed = time.perf_counter() - started
        if decided >= len(ids):
            return int(decided), elapsed
        if elapsed > DRAIN_TIMEOUT_SECONDS:
            return int(decided), elapsed
        print(f"    {decided}/{len(ids)} matched ({elapsed:5.1f}s)", end="\r", flush=True)
        await asyncio.sleep(1.0)


SETTLE = """
    SELECT extract(epoch FROM (decided.occurred_at - received.occurred_at)) AS seconds
      FROM audit_event received
      JOIN match_result m ON m.document_id = received.entity_id
      JOIN audit_event decided
        ON decided.entity_id = m.id AND decided.action = 'match.decided'
     WHERE received.action = 'document.received'
       AND received.entity_id = ANY($1::uuid[])
"""


async def measure(document_ids: list[str], connection: asyncpg.Connection) -> list[float]:
    rows = await connection.fetch(SETTLE, [uuid.UUID(d) for d in document_ids])
    return [float(r["seconds"]) for r in rows if r["seconds"] is not None]


async def queue_depth(connection: asyncpg.Connection) -> dict[str, int]:
    rows = await connection.fetch("select state, count(*) c from job group by 1")
    return {r["state"]: int(r["c"]) for r in rows}


async def run(count: int, concurrency: int, as_json: Path | None) -> int:
    print(f"Threshold load test: {count} invoices, {concurrency} concurrent\n")

    tmp = Path(__file__).resolve().parent / ".load"
    tmp.mkdir(exist_ok=True)
    print("  rendering invoices...", end=" ", flush=True)
    render_started = time.perf_counter()
    payloads = [
        render_invoice(tmp / f"{i}.pdf", f"LOAD-{uuid.uuid4().hex[:10].upper()}")
        for i in range(count)
    ]
    print(f"{time.perf_counter() - render_started:.1f}s")

    connection = await asyncpg.connect(OWNER_DSN)
    try:
        before = await queue_depth(connection)
        print(f"  queue before: {before or 'empty'}\n")

        gate = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient() as client:
            wall_started = time.perf_counter()
            results = await asyncio.gather(*(submit(client, payload, gate) for payload in payloads))
            wall = time.perf_counter() - wall_started

        accepted = [(d, e) for d, e, _status in results if d is not None]
        rejected = [s for d, _e, s in results if d is None]
        duplicates = sum(1 for d, _e, s in results if d is not None and s == 200)
        document_ids = [d for d, _ in accepted]
        accept_latencies = [e for _, e in accepted]

        print(
            f"  accepted {len(accepted)}/{count} in {wall:.1f}s "
            f"({len(accepted) / wall:.1f}/s submitted)"
        )
        if duplicates:
            print(f"  {duplicates} were already known (200), and enqueued nothing")
        if rejected:
            print(f"  rejected {len(rejected)}: statuses {sorted(set(rejected))}")

        print("\n  draining…")
        decided, drain_seconds = await drain(connection, document_ids)
        print(f"    {decided}/{len(document_ids)} matched in {drain_seconds:.1f}s          ")

        settle_latencies = await measure(document_ids, connection)
        after = await queue_depth(connection)

        report = {
            "count": count,
            "concurrency": concurrency,
            "accepted": len(accepted),
            "rejected": len(rejected),
            "duplicates": duplicates,
            "matched": decided,
            "submit_wall_seconds": round(wall, 2),
            "submit_rate_per_second": round(len(accepted) / wall, 2) if wall else None,
            "drain_seconds": round(drain_seconds, 2),
            "pipeline_rate_per_second": (
                round(decided / (wall + drain_seconds), 2) if (wall + drain_seconds) else None
            ),
            "accept": summarise("accept", accept_latencies),
            "settle": summarise("settle", settle_latencies),
            "queue_after": after,
        }

        print()
        for stage in ("accept", "settle"):
            s = report[stage]
            assert isinstance(s, dict)
            label = "accept (POST -> 202)" if stage == "accept" else "settle (received -> matched)"
            if not s["n"]:
                # A stage with no samples is a finding, not a formatting problem:
                # it means the audit rows this measures from were not written.
                print(f"  {label:<32} no samples")
                continue
            print(
                f"  {label:<32} n={s['n']:<4} "
                f"p50 {s['p50_ms']:>8.1f}ms  p95 {s['p95_ms']:>9.1f}ms  "
                f"p99 {s['p99_ms']:>9.1f}ms  max {s['max_ms']:>9.1f}ms"
            )
        print(
            f"\n  end to end: {decided} invoices matched in "
            f"{wall + drain_seconds:.1f}s = {report['pipeline_rate_per_second']}/s"
        )
        print(f"  queue after: {after or 'empty'}")

        if as_json:
            as_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\n  wrote {as_json}")

        if decided < len(document_ids):
            print(
                f"\n  {len(document_ids) - decided} never matched within "
                f"{DRAIN_TIMEOUT_SECONDS:.0f}s. That is a result, not a crash: check "
                "`make queue-stats` and `make dlq`.",
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        await connection.close()
        for pdf in tmp.glob("*.pdf"):
            pdf.unlink()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=60, help="invoices to submit")
    parser.add_argument("--concurrency", type=int, default=8, help="submissions in flight")
    parser.add_argument("--json", type=Path, default=None, help="write the report here")
    arguments = parser.parse_args(argv)
    if arguments.count < 1 or arguments.concurrency < 1:
        parser.error("--count and --concurrency must be positive")
    return asyncio.run(run(arguments.count, arguments.concurrency, arguments.json))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
