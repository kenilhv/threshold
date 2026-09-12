# Nobody is reporting the queue

**Alert:** `QueueObserverStopped` (page)

Queue depth has not been published for five minutes. This alert is a page and
it deserves to be, because it means two things at once: probably nothing is
draining the queue, and certainly **no other queue alert can fire**.
`QueueBacklogGrowing`, `OldestJobStuck` and `DeadLettersUnreplayed` all read
series that have stopped arriving. Silence from them is not good news right now.

## Why absence means this, and nothing else

Queue depth is published by exactly one worker — whichever holds the reaper's
advisory lock — because depth is a property of the queue, and eight workers
reporting it would make `sum()` report eight times the truth.

Two design choices make absence unambiguous:

- The gauge reports **zero** for an empty queue rather than reporting nothing,
  so "no work waiting" cannot look like "no observer" (D-024).
- The snapshot **expires after 90 seconds**, so a worker that stops looking
  stops claiming to know rather than freezing its last reading on the dashboard
  for ever.

Metrics are pushed, not scraped, so there is no `up` series and no scrape
failure to look at. This alert is the whole mechanism by which a dead worker is
noticed (D-024).

## Check the pool

```bash
docker compose ps worker
docker compose logs worker --since 10m | tail -40
```

**No worker running.** Start it. Nothing was lost: a claim is a state change,
not a delete, so every job a dead worker held is still in the table and the
reaper returns it once a live worker takes the lock.

**Worker running, logging `worker.reaper_failed`.** The observation runs inside
the reaper loop, so a database error there stops both reaping and reporting. The
traceback is in the log line.

**Worker running, logging nothing at all.** It is wedged. The claim loop retries
with backoff on failure and logs `worker.claim_failed` each time; total silence
means it is blocked somewhere that does not time out. Restart it — the lease
expires and the reaper returns whatever it held.

```bash
docker compose restart worker
```

## Check it is not the telemetry rather than the worker

The worker can be perfectly healthy and simply unable to export.

```bash
docker compose logs worker --since 10m | grep -E "metrics\.(exporting|local_only)"
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:9090/api/v1/otlp/v1/metrics
```

`metrics.local_only` means no endpoint is configured and nothing has ever been
exported. A `404` from that path means Prometheus is running **without**
`--web.enable-otlp-receiver`, in which case every service has been pushing into
a void while looking perfectly healthy from the inside. `make smoke` checks that
flag for this reason.

## Confirm recovery

```bash
make queue-stats                  # the truth, straight from Postgres
python scripts/check_metrics.py   # 0 when every declared metric is arriving
```

Queue depth reappears within one observation cycle — 30 seconds — of a worker
taking the lock.

## Where it shows

Grafana → [Threshold](http://localhost:3001/d/threshold-overview) → **Queue
depth**. The gap in the series is the outage.
