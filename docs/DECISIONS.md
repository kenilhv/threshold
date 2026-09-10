# Decision record

One entry per decision that would be expensive to reverse, or that a reader
would otherwise have to reconstruct from the code. Newest first.

---

## D-015 — Using the extracted values is what proved they were wrong

**Date:** 2026-09-10 · **Status:** accepted

The extraction tier shipped in M1 with tests passing. Every one of those tests
asserted that a field was read; none asserted it was read *correctly against
something that would notice*. The first invoice put through the matcher came
back with three defects, all of which had been there since M1:

- **The purchase-order number lost its prefix.** `PO-44718` parsed as the label
  `PO` followed by the value `-44718`, the token pattern dropped the leading
  hyphen, and the field came out `44718` — a number matching no order on file.
  A label is only a label when something separates it from the value, so the
  label pattern now requires that separator.
- **The VAT rate was being read as the VAT amount.** The line prints as
  `zzgl. MwSt. 19,00 %` with the amount `58,29` on the following line, so the
  rate is nearer the label and scores higher. The invoice came out carrying
  19.00 of tax instead of 58.29. A percentage is never a money amount, whatever
  line it sits on.
- **There were no line items at all.** `line_items=()` with a comment deferring
  it to M3. A three-way match with no lines has no price to compare and no
  quantity to compare, which is the entire check.

The first two were caught by the arithmetic corroboration already in the
extractor — subtotal plus tax did not reconcile, so confidence dropped and the
invoice blocked rather than paying. The mechanism worked. It just could not say
*which* number was wrong, and nothing downstream was consuming the value closely
enough to find out.

The general lesson is not about extraction. A field that is stored and never
read is not verified by being stored, and a test that asserts a value exists
verifies less than it appears to. These bugs survived a milestone because the
only consumer was a JSONB column.

Line-item extraction reads a record's numbers from the *end*. `extract_text`
flattens a table to one line per cell, so a wrapped description like
"…boxed / 100" puts a stray number in the middle of the record; taking the first
three numbers reads a quantity of 100 and a unit price of 12. The last three are
always quantity, unit price and line total, because they are the last three
columns. Records are separated by position numbers, identified as the
subsequence that counts 1, 2, 3… — the stray `100` looks exactly like a position
otherwise, and was splitting every record in half.

Anything not fitting that shape yields no line items rather than guessed ones,
and the matcher records "no line items" as a skipped check. That is the point:
before this, an invoice with no lines matched clean, because there was nothing
to disagree with. An invoice that was never compared must not read the same as
one that was compared and passed.

---

## D-014 — The auto-approval gate is provisional, and says so

**Date:** 2026-09-10 · **Status:** accepted · **Has a known, unpaid cost**

`minimum_field_confidence` is 0.50. It is the weakest number in the system and
the one most likely to be wrong.

A gate is a probability threshold: below *this likelihood of being right*, do
not act without a human. The deterministic tier does not produce probabilities.
It produces an ordinal signal — 0.88 means "found immediately after its label",
0.62 means "found on the following line", 0.45 is a hardcoded admission that
finding an unlabelled vendor name near the top of a page is guesswork, and 0.0
means "not found". None of those numbers has been measured against how often
that reading is actually correct.

So any threshold is a guess. The honest response is to pick the only boundary
those numbers genuinely support — between "read something" and "read nothing" —
and to say plainly that it is provisional. M6 builds the calibration curve, and
this number has to be re-derived from it rather than tuned until the demo looks
good. Tuning it against a fixture is how a control becomes a decoration.

Two things follow from taking that seriously:

**Vendor identity is corroborated, not scored.** The extractor rates the vendor
name 0.45 by construction. But once that string has matched a row in the vendor
master, the identity no longer rests on the heuristic — an independent source
agreed. So the matcher drops `vendor_name` from the gate *when resolution
succeeded*, which is the same argument the extractor already makes when subtotal
plus tax reconciles to the total. A name that fails to resolve is not discounted
at all: it has already raised `VENDOR_UNKNOWN`, which blocks.

**The stored score is left alone.** `Extraction.lowest_decision_confidence`
still records the raw reading. The corroborated value is a different question
asked at decision time, and collapsing the two would destroy the number M6 needs
to calibrate against.

The unpaid cost is plain: until M6, this system's auto-approval boundary rests
on a number nobody has validated. Every other threshold here can be argued from
the domain. This one cannot yet.

---

## D-013 — Bank-detail changes are detected but not yet detectable

**Date:** 2026-09-10 · **Status:** accepted · **Has a known, unpaid cost**

`BANK_DETAILS_CHANGED` is in the taxonomy, routes to treasury with the shortest
clock in the system, and is covered by the match table. `MatchingService` sets
its flag to `False` unconditionally, so in the running pipeline it can never
fire.

The reason is a gap in the middle. `VendorBankAccount` keeps payment details as
history specifically so "changed since we last paid them" is answerable, and the
invoice fixtures render a masked IBAN on the page. But `ExtractedInvoice` has no
field for payment details and the extractor does not read them, so there is
nothing to compare the history against.

This is recorded rather than quietly worked around because it is the flagship
control. The architecture document calls a vendor bank change the
highest-severity exception the system knows about, and it is the shape almost
every real accounts-payable fraud takes. A reader who sees the code in the
taxonomy is entitled to assume it fires.

Closing it needs three things: a payment-details field on the extraction schema,
a parser for the masked IBAN and sort-code shapes the layouts render, and a
comparison against the current `VendorBankAccount` fingerprint. None of it is
hard. It was left out of M3 rather than half-done inside it, because a detector
wired to a field nobody populates is worse than no detector — it reads as
covered.

---

## D-012 — The queue tests own their database, and `claim` sorts what it returns

**Date:** 2026-09-09 · **Status:** accepted

Two findings, one cause: the queue tests had been sharing a database with a
live worker.

`make up` leaves a worker polling the application database once a second. It is
a real consumer running the same claim query, so it competes for jobs a test
just enqueued and then fails them, because their payloads name documents that do
not exist. Every assertion about who claimed what, how many attempts a job has,
and what reached the dead-letter table was only holding because the worker
happened to be down. A suite that passes because another process is broken is
not passing.

So `tests/integration/test_queue.py` provisions `threshold_queue_tests`, migrates
it with Alembic and runs there. Alembic rather than `Base.metadata.create_all()`:
the partial indexes are hand-written in `0003_job_queue` because SQLAlchemy
cannot express their predicates, so a schema built from the models would quietly
omit `uq_job_dedupe_key` — the exact constraint several of those tests exist to
verify. The API tests keep using the application database, because they *want* a
live worker: that is the thing they are testing.

Running on a fresh database immediately exposed a real bug the shared one had
been hiding. `claim()` selects with `ORDER BY run_after, created_at` inside the
subquery, which decides *which* rows are taken — but `UPDATE … RETURNING` makes
no promise about the order rows come back in, and Postgres returns them roughly
in heap order. On a table that has seen deletes and is reusing free space, heap
order stops matching insertion order and a batch comes back shuffled. The queue
was FIFO in the part nobody observes and not FIFO in the part a worker actually
sees. `claim()` now re-sorts its result.

Neither of these is exotic. Both are the ordinary consequence of testing against
a database that something else is also using, which is worth the cost of the
extra database on its own.

---

## D-011 — A failed claim retries in-process; only a crash needs Docker

**Date:** 2026-09-09 · **Status:** accepted

Found by running `make migrate-roundtrip` against a live stack. The downgrade
dropped the `job` table underneath a polling worker, the claim raised
`UndefinedTableError`, the exception escaped `run_forever`, and the process
exited. Compose leaves `restart` at its default, so the container stayed down.
The API went on accepting documents and queueing jobs that nobody would ever
run, and nothing anywhere said so.

The schema vanishing is not the interesting part — that was self-inflicted. The
same path is a Postgres restart, an RDS failover, a connection reset, or a
migration running during a deploy, all of which are routine, all of which are
brief, and any of which would have permanently drained the pool.

So the claim loop now catches, logs, and backs off exponentially from the poll
interval to a thirty-second cap, resetting on the first success. This is safe in
a way that retrying is usually not: a claim that raised has claimed nothing, so
there is no partial work to reconcile and no lease to lose. `_execute` already
settled its failures against the job and `_reap_loop` already guarded itself —
the claim path was simply the one that never got the treatment.

`restart: unless-stopped` on `api` and `worker` is the backstop for what
in-process handling cannot cover, which is a crash. It is deliberately not the
mechanism: a pool that needs Docker to notice a ten-second blip has already
dropped whatever it was holding, and on ECS or Kubernetes the supervisor's
reaction time is worse, not better. The one-shot `migrate`, `seed` and
`minio-init` services keep `restart: "no"` — a job that reruns itself is not a
job.

Two things this deliberately does not do. It does not distinguish a transient
database error from a permanent one, because at the claim site that distinction
cannot be made honestly — `UndefinedTableError` is permanent during a downgrade
and transient during a deploy, and the same exception carries both. And it does
not give up after N consecutive failures, so a worker pointed at a database that
will never come back retries until someone stops it. Both are the right trade
while the failure is loud in the logs; neither survives contact with a system
nobody is watching, which is what M5's alert rules are for.

Regression test: `TestPoolSurvivesADatabaseOutage` in
`tests/integration/test_queue.py`. It wraps the real queue so the first three
claims raise, then asserts the worker was still claiming afterwards and the job
ran. Reverting the guard makes it fail.

---

## D-010 — A dead worker's attempt is not counted against the job

**Date:** 2026-09-09 · **Status:** accepted

`reap_expired_leases()` returns a job to `pending` and clears the claim, but
deliberately does not increment `attempts`. The claim already incremented it;
counting it again would charge the job twice for one try.

The reason is whose fault the failure was. A job that fails three times because
the document is unreadable has used its budget legitimately. A job whose worker
was `SIGKILL`ed by an OOM killer has not failed at all — the infrastructure
did — and halving its remaining retries punishes the work for something it had
no part in. In a pool that is being rolled or rescheduled, that difference is
the difference between a deploy that is invisible and a deploy that dead-letters
a slice of the queue.

`scripts/demo_crash_recovery.sh` asserts this directly: it prints `attempts`
before and after the reap and calls out that it is still 1.

The cost is that a job which reliably kills its worker — a payload that
segfaults a parser, say — is retried forever rather than dead-lettering. That is
the right trade at this size, because such a job also takes the worker down and
so becomes visible immediately, but it is a real hole and a poison-pill detector
belongs here before this runs unattended.

---

## D-009 — Deduplication is a partial unique index, not a lookup

**Date:** 2026-09-09 · **Status:** accepted · **Has a known, unpaid cost**

`enqueue(dedupe_key=…)` does not check for an existing job and then insert.
Between the check and the insert, another worker can insert — the classic
read-modify-write race, which under concurrency produces exactly the duplicate
the key exists to prevent. Instead the uniqueness is a database constraint and
the insert absorbs the collision:

```python
base = insert(Job).values(**values)
if dedupe_key is not None:
    base = base.on_conflict_do_nothing(
        index_elements=[Job.dedupe_key],
        index_where=text("dedupe_key IS NOT NULL AND state IN ('pending', 'claimed')"),
    )
```

The index is partial, and that is the whole design: a `dedupe_key` is unique
only among jobs that are still `pending` or `claimed`. Once a job succeeds or
dies, the key is free again, so "do not run this twice concurrently" does not
silently become "never run this again", which would make a retry after a failure
impossible.

Two things here were bugs before they were decisions, and both are the kind that
only appear under load:

- `index_where` is not optional. Without it Postgres cannot match the partial
  index and raises `InvalidColumnReferenceError: there is no unique or exclusion
  constraint matching the ON CONFLICT specification`. Every deduplicated enqueue
  would have failed — and the unit tests, which exercise the in-memory queue,
  would not have noticed.
- `on_conflict_do_nothing()` must be applied **before** `.returning()`. The
  other order compiles and silently drops the conflict clause.

The unpaid cost is the flip side of freeing the key on success: re-submitting
the same document after its extraction finished enqueues a second, redundant
extraction. The queue is right to allow it; the caller has to decide. So
`IngestService` checks for an existing extraction and skips the enqueue rather
than the queue pretending the work is still in flight.

---

## D-008 — The enqueue commits with the state change, or not at all

**Date:** 2026-09-09 · **Status:** accepted

`Queue.enqueue()` takes the caller's `AsyncSession` rather than opening its own.
Ingestion therefore writes the `document` row and the `job` row in one
transaction, and either both land or neither does.

This is the transactional outbox, and it is the reason there is no broker here.
With SQS or RabbitMQ, "insert the row" and "publish the message" are two systems
and there is no commit that spans them: publish first and a rollback leaves a
message about a document that does not exist; publish second and a crash between
them leaves a document nobody will ever process. Every mitigation for that —
outbox tables, two-phase commit, reconciliation sweeps — is machinery to buy back
what one database gives away for free.

The cost is stated plainly in the architecture doc: this does not fan out, it
does not replay a topic, and the polling loop becomes the bottleneck somewhere
above a few hundred jobs per second. Accounts payable is hundreds of invoices
per **day**. A `Queue` protocol marks the seam, and `PostgresJobQueue` is its
only implementation — there is no SQS adapter here, and moving to one would not
be free, because the single commit this decision buys is exactly the thing a
broker cannot give back. The protocol makes that cost visible at the type level
rather than leaving it to be discovered.

One consequence worth knowing before it confuses someone: jobs enqueued inside a
single transaction share one `now()`, because Postgres freezes
`CURRENT_TIMESTAMP` per transaction. Their `run_after` values are identical and
their relative claim order is therefore undefined. Nothing depends on it, but a
test that assumes FIFO within a batch will flake.

---

## D-007 — Claiming a job is a state change, never a delete

**Date:** 2026-09-09 · **Status:** accepted

A worker claims a job by updating it — `state = 'claimed'`, `claimed_by`,
`claimed_at`, `lease_expires_at`, `attempts + 1` — inside
`SELECT … FOR UPDATE SKIP LOCKED`. It never deletes the row and never holds the
job in process memory.

`SKIP LOCKED` is what makes the pool scale without coordination: a worker whose
candidate row is already locked steps over it rather than queueing behind it, so
N workers do not serialise on the head of the queue. The integration suite runs
four workers against forty jobs and asserts every job was claimed exactly once.

The delete-on-claim alternative is the more obvious design and it is why so many
homegrown queues lose work. Once the row is gone, the only record that the job
exists is in the memory of the process holding it, and a `SIGKILL` — an OOM
kill, a node preemption, a `docker compose kill` — takes that record with it.
There is nothing left to recover from.

Because the row survives, recovery is just noticing that a lease expired. The
reaper (running under advisory lock `0x74855170`, so exactly one worker sweeps
regardless of pool size) returns any job whose `lease_expires_at` is in the past
to `pending`, with its payload and attempt history untouched.

The cost is that the queue is a table that grows and needs a retention policy,
and that every claim is a write, so the queue is not free at high throughput.
Both are acceptable at this volume; neither is a correctness problem.

---

## D-006 — Store the object before writing the row

**Date:** 2026-09-09 · **Status:** accepted · **Has a known, unpaid cost**

Ingestion hashes first, writes to object storage second, inserts the document
row third. The reverse order would leave a row pointing at bytes that were never
stored — a dangling reference that surfaces much later as an unexplained 404 in
a reviewer's console, by which point the document is simply gone.

This ordering trades that for the opposite failure: when extraction raises and
the transaction rolls back, the object survives with no row referring to it. The
integration suite demonstrates it — a deliberately corrupt PDF returns 422, and
the run ends with six objects against five document rows.

An orphan wastes storage and is reclaimable by diffing bucket keys against
`document.storage_key`. A row promising bytes nobody has is unrecoverable. The
trade is deliberate, but **the reclaiming sweep does not exist yet** and should
land before this runs unattended for any length of time.

---

## D-005 — Idempotency at two layers, answering different questions

**Date:** 2026-09-09 · **Status:** accepted

`Idempotency-Key` answers *"is this the same request?"* — a client retrying
after a timeout gets its original response rather than a second document. The
content hash answers *"are these the same bytes?"* — a supplier resending a PDF
under a new filename resolves to one document with no key involved.

Either alone leaves a hole. A retry with a fresh key creates a duplicate without
the hash; two genuinely different documents sent under one key collide without
the fingerprint.

The header follows Stripe's published semantics, including the case
implementations usually miss: a key reused for *different* content is a 409,
never a silent replay. Handing a caller another invoice's id under a 200 gives
it no way to detect the mistake.

A third layer arrives in M8 as a unique constraint on the payment row. The three
catch a retried HTTP request, a redelivered queue message, and a logic error
respectively. None is redundant with the others.

---

## D-004 — Confidence is per field, and the gate reads the minimum

**Date:** 2026-09-09 · **Status:** accepted

A document-level confidence score cannot answer the only question that matters
operationally: *may this specific value be used to make a decision?* An invoice
can have a perfectly legible total and an illegible purchase-order number, and
those two facts route differently.

So every extracted field carries its own confidence, and the auto-approval gate
reads the **minimum** across decision-relevant fields — never a mean. A mean
lets four confident fields carry one unreadable total past the threshold, which
is precisely the failure this system exists to prevent.

The threshold itself is currently a constant named `PROVISIONAL_CONFIDENCE_GATE`
so that its placeholder status is visible at every call site. M6 replaces it
with an operating point derived from a calibration curve.

---

## D-003 — A deterministic extraction tier that earns its confidence

**Date:** 2026-09-09 · **Status:** accepted

The always-available tier reads the PDF text layer and matches labelled
patterns. It is genuinely worse than a vision model and is labelled worse
everywhere its output is stored.

It exists so CI exercises the real pipeline — ingest, extraction, persistence
and the confidence gate — with no weights, no API key and no network. The
alternative is mocking away the one component the system is built around. It is
also the floor any model tier is measured against, which turns "the model helps"
into a number.

Its confidence is derived from observable signal rather than returned as a
constant: label adjacency, arithmetic corroboration when subtotal plus tax
reconciles to the total, a penalty when several candidates compete for one
field, and lower confidence for genuinely ambiguous date formats. A hard-coded
0.9 would be decoration that M6's calibration work would immediately expose.

---

## D-002 — Immutable, versioned extractions

**Date:** 2026-09-09 · **Status:** accepted

Re-extracting a document inserts version N+1 rather than mutating N, enforced by
a database trigger rather than by convention.

This is what makes the evaluation harness possible: any historical document can
be replayed against a new model or prompt and the two rows diffed. Without it,
"the model changed and the numbers moved" is a mystery rather than a query.

---

## D-001 — Migrations are their own unit of work

**Date:** 2026-09-09 · **Status:** accepted

`migrate` is a separate compose service gated on
`condition: service_completed_successfully`, never an application-startup side
effect. Running migrations at startup means two API replicas race to migrate the
same database, which is a real outage and an easy one to avoid.

Every migration is reversible and `make migrate-roundtrip` proves it by running
upgrade → downgrade → upgrade. A migration nobody dares reverse is a migration
that gets reversed by hand at 3am.
