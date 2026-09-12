# The API is failing or slow

**Alerts:** `ApiErrorRate` (page) · `ApiLatencyHigh` (page)

Either more than one request in twenty is returning a server error, or p95
latency is above a second. Both are pages because the console and every
integration talk to this API, and an invoice that cannot be uploaded is an
invoice that does not exist yet.

## What normal looks like

Measured: `/healthz` 3 ms, `/readyz` 6 ms, the match endpoint 7 ms, with a tail
around 25 ms. One second is roughly forty times the healthy p95. This is not a
slow day; it is a different system.

## Ask readiness first

```bash
curl -s localhost:8000/readyz | python -m json.tool
```

It names every dependency, says which are `required`, and tells you which one is
down. Two of the three answers end the investigation immediately:

**`postgres` not ok.** Everything is a query. Check the database before
anything else.

**`object_store` not ok.** Uploads fail at the first step, because the bytes are
stored before the row that describes them (D-006). Reads of stored documents
fail too. The bucket or its credentials are the problem.

**`identity` not ok.** This is **not** the cause of a 5xx rate, and readiness
stays `true` when it happens — by design (D-025). It means this instance holds
no cached signing keys, so authenticated calls will return 503 until it can
reach the identity provider. If the error rate is 401s and 503s on
authenticated routes only, this is your answer and the fix is at Keycloak, not
here.

## If readiness is clean

Then the dependency is up and something else is consuming it.

**Connection pool.** Grafana → **Connection pool**. If `used` is pinned at the
pool size, requests are queueing for a connection and latency climbs while
errors stay low — the classic shape of this alert firing on latency first. The
worker and the API share a database; check the queue panels too.

**Slow queries.** The trace has the answer. Take a trace id from any recent log
line and open it in Tempo:

```bash
docker compose logs api --since 10m | grep trace_id | tail -1
```

Every SQL statement is a span. A single `SELECT` holding the whole request is
visible immediately — which is only true since D-023, when the SQL spans
started being emitted at all.

**A dependency that is up but slow.** MinIO answering `head_bucket` in
milliseconds does not mean `put_object` is fast. Look at the `S3.PutObject`
spans in the same trace.

## Where it shows

Grafana → [Threshold](http://localhost:3001/d/threshold-overview) → **API
request rate**, **API latency**, **API errors**, **Connection pool**.
Traces: [Tempo](http://localhost:3200) via Grafana's Explore.
