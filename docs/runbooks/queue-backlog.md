# The queue is behind

**Alerts:** `QueueBacklogGrowing` (ticket) · `OldestJobStuck` (page)

Invoices are arriving faster than they are being processed, or something is
returning work to `pending` without finishing it. Nothing is lost — a job only
leaves the table when it succeeds or dies — but every waiting job is an invoice
nobody has looked at.

## What normal looks like

Measured over 317 completed jobs: a median wait of **0.503 s** and a maximum of
**1.084 s**, which is the one-second poll interval sampled uniformly. Steady
state pending depth is **zero**. The slowest a healthy system gets is about
three minutes — two backoffs of five and ten seconds, plus a two-minute lease
and a thirty-second reaper after a worker dies mid-job.

So: five minutes of age is outside the design, and a hundred pending jobs
sustained for ten minutes is a pool that is not keeping up.

## Decide which of the three it is

```bash
make queue-stats
```

**1. Depth is high and the oldest job is young.** Arrival outran the pool.
Nothing is broken; there is more work than workers. Check whether something
uploaded in bulk.

**2. Depth is high and the oldest job is old.** Work is going round in a
circle. Look at the "Job outcomes" panel: if `retrying` is climbing, jobs are
failing and being put back. Find out why before adding workers — more workers
retrying a poison payload faster is not progress.

```bash
docker compose logs worker --since 15m | grep -E "job_failed|retry_scheduled"
```

**3. Depth is high and nothing is being claimed at all.** Check
`QueueObserverStopped` first — if it is also firing, the pool is dead and this
is [telemetry-blind.md](telemetry-blind.md). If the depth metric is still
arriving, one worker is alive but wedged; look for a job that claimed and never
settled.

```bash
make reap   # returns jobs whose worker died holding the lease
```

## What not to do

**Do not clear the queue.** Every row is an invoice somebody sent. `make reap`
returns abandoned work; there is no supported way to discard it, on purpose.

**Do not add workers before knowing which case this is.** In case 2 it
multiplies the failure rate without moving the backlog.

## Where it shows

Grafana → [Threshold](http://localhost:3001/d/threshold-overview) → **Queue
depth**, **Queue wait**, **Job outcomes**.
