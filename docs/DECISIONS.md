# Decision record

One entry per decision that would be expensive to reverse, or that a reader
would otherwise have to reconstruct from the code. Newest first.

---

## D-019 — The queue trusted the worker's clock

**Date:** 2026-09-10 · **Status:** accepted · **Fixed**

`models.py` has said since M0 that timestamps "are set by the database, not the
app, so two workers on skewed clocks cannot disagree about ordering." The queue
did not honour it. `claim()` computed `lease_expires_at` from the worker's own
clock, `fail()` computed the retry `run_after` the same way, and both were then
compared against the database's `now()` — in the claim predicate and in the
reaper. Every lease was only as good as the agreement between two clocks.

That agreement is not something to assume on this stack. The Docker VM had just
come back from the host sleeping, and WSL2 clocks are known to drift after
resume. A worker running a few minutes behind would write leases that were
already expired as far as the database was concerned; the reaper would return
live jobs to `pending` and a second worker would run them. That is double
processing, in a system whose next milestone moves money.

It surfaced as a test that passed alone and failed in the full run.
`test_attempts_are_exhausted_then_dead_lettered` wrote the host's `now()` into
`run_after` and claimed in the same transaction, where Postgres's `now()` is the
transaction start — so it passed or failed on sub-millisecond skew, depending on
load. The measured skew at the time was about a millisecond. Fixing the test
alone would have made the gate green and left production with the same defect,
so the fix is in the queue: every timestamp it writes and later compares —
`claimed_at`, `lease_expires_at` on claim and heartbeat, the retry `run_after`,
`replayed_at`, the purge cutoff — now comes from `func.now()`. The module has no
client-clock timestamps left. `fail()` returns the stored retry time rather than
logging its own estimate of it.

`TestClockSkew` pins it by making the queue module's clock run ten minutes slow.
With the old code the reaper takes the live job; verified by reverting the one
line and watching it fail. With the fix the lease holds, and a retry cannot be
scheduled into the past.

The general point is the same one D-015 made about stored values: a guarantee
written in a docstring is not a guarantee until something breaks when it is
violated.

---

## D-018 — The API answered before it committed

**Date:** 2026-09-10 · **Status:** accepted · **Fixed**

Found while checking the console, not while writing the API, which is the
uncomfortable part. Approving an invoice and reading its decisions back in the
same breath came back empty in **6 of 8 trials**, and populated half a second
later. Every write endpoint in this service had the same shape.

`get_session` commits in its exit code, after the `yield`. A `yield` dependency
in FastAPI defaults to *request* scope, which runs that exit code after the
response has been sent. So the order was: route returns, `201` goes out, then
the row is written.

The visible half is a read-your-writes failure, and in this console it is not
cosmetic — it redirects to the invoice straight after a decision, so it could
render a just-approved invoice as undecided, form and all, and invite a second
decision. The invisible half is worse: a commit that failed at commit time — a
constraint, a dropped connection — left the client holding a `201` for a
decision that was never recorded.

`SessionDep` is `Depends(get_session, scope="function")`: the commit runs when
the route returns and before the response is written, so a failure becomes a
`500` and a success is readable the moment the client has it. Measured after:
**0 of 12**. Two tests hold it:

- `test_session_scope.py` walks every route, recursively through
  sub-dependencies, and fails if any takes a session any other way. Its first
  version passed while checking **zero routes**: FastAPI 0.141 no longer
  flattens included routers into `app.routes`, so a flat scan finds nothing and
  reports no offenders. A second test now asserts the scan found the routes it
  should, so a future change of shape turns red instead of quietly becoming a
  no-op. Both were proven by reverting one route and watching the first fail.
- `TestTheResponseFollowsTheCommit` repeats the original measurement against the
  live stack, with no sleep, six times.

The integration suite had been green throughout, because every test that wrote
something then polled for it. Polling is correct client behaviour for
asynchronous work, and it was hiding a synchronous bug.

---

## D-017 — A person decides

**Date:** 2026-09-10 · **Status:** accepted · **Has known, unpaid costs**

M4 turned `disposition` from a string nothing read into something consequential.
The calls that were genuinely contested:

**You may decide an invoice only if you own every open exception on it.** The
obvious rule, "the owner of the worst exception", is wrong in a specific and
dangerous way. Exceptions sort by severity and then by code, so among blocking
codes the alphabet breaks the tie, and `arithmetic_mismatch` (ap_clerk) sorts
ahead of `bank_details_changed` (treasury). An altered total plus redirected
remittance is the likeliest composite fraud in this domain, and a worst-owner
rule hands it to a clerk who can clear treasury's finding without treasury ever
seeing it. `test_approval.py` guards the premise itself, so the test cannot
quietly stop testing anything if the taxonomy changes.

The consequence is stated rather than hidden: an invoice whose exceptions have
two different owners cannot be approved by one person, because there is no
multi-party approval yet. It stays blocked and says which roles it needs.

**Decisions attach to a `match_result`, not a document.** A decision is about one
body of evidence. When a new version lands, the old approval does not refer to
it, and nothing needs invalidating.

**`auto_approved` and `held` are not terminal.** Only approve and reject are,
enforced by a partial unique index. A terminal auto-approval would make D-016's
documented worst gap — an unreadable payment block auto-approves — permanently
unreachable by a person.

**The console is a back-end for front-end.** The browser never holds a token;
the console exchanges the code server-side and keeps the token in an httpOnly
cookie. It authorises payment and renders vendor-supplied text, and a single XSS
in a public-client console would hand out a bearer token for it. PKCE is written
by hand rather than through a library, so the four things that matter — `state`,
the verifier, server-side exchange, and the public/internal issuer split — are
visible to a reader rather than behind an import.

**The realm was reconciled to the taxonomy, never the reverse.** `receiving` and
`treasury` owned exceptions and were not realm roles. Renaming an `OwnerRole`
would have been the smaller diff and would have rewritten the owner stamped on
every exception already stored.

Found and fixed while verifying, each of which would have failed quietly:

- **The stack came back half-up after a Docker VM restart.** `api`, `worker`
  and `console` had `restart: unless-stopped`; Postgres, Redis, MinIO and
  Keycloak had no policy, exited together with code 255, and stayed down for
  eleven hours while the API reported healthy — correctly, since liveness is not
  readiness, and uselessly. Every long-running service now restarts.
- **The console's healthcheck tested the wrong thing.** It fetched `/queue`,
  which redirects to login, which redirects to Keycloak's *public* URL. `fetch`
  follows redirects, so it was asking whether `localhost:8080` was reachable from
  inside the container, which it never is. The console reported unhealthy while
  serving correctly. It now has a liveness route of its own.
- **Backslashes collapsed in inline edits on this shell.** Several lines of
  `smoke.sh` had acquired raw escape bytes and literal newlines across the last
  few milestones, including a `tr -d '\r'` that had become `tr -d '<newline>'`.
  Scripts are now written from files, and `smoke.sh` contains no control bytes.

**Unpaid costs:**

- **Readiness checks the wrong dependencies.** It checks Postgres and Redis.
  Nothing in the application uses Redis; MinIO (ingest) and Keycloak (every
  authenticated route) are used and not checked, so with only Keycloak down,
  readiness says ready while authenticated calls return 503. The smoke check was
  renamed to say what it actually checks rather than "every dependency". M5 owns
  the fix, which includes deciding whether a shared IdP belongs in readiness at
  all — pulling every instance out of rotation during an IdP blip is often worse
  than serving a clear 503.
- **Worker audit rows have no trace id.** A worker has no request to correlate
  to. M5 propagates a trace through the queue payload.
- **No multi-party approval**, so composite invoices with two owners cannot be
  cleared in M4, and no segregation of duties between whoever resolves an
  exception and whoever approves.
- **The console has no browser-driven test.** `scripts/check_console.py` drives
  it server-side as two roles, including a no-JavaScript submission, but nothing
  runs a real browser against it. Typing a password into Keycloak's form from an
  automated agent was deliberately not done; a person signing in is the
  verification that remains.

---

## D-016 — Payment-detail comparison, and the four ways it can decline to answer

**Date:** 2026-09-10 · **Status:** accepted · **Closes D-013** · **Has a known, unpaid cost**

`BANK_DETAILS_CHANGED` now fires. An invoice that is arithmetically perfect —
right order, right price, goods received — is blocked to treasury on a four-hour
clock when it asks to be paid into an account this vendor has never used.
`make demo-match` shows it against a real PDF.

**Tail-to-tail, not the fingerprint.** D-013's written plan named
`VendorBankAccount.account_fingerprint`, and that plan was not executable: the
fingerprint is an HMAC over the *full* routing and account numbers, while every
one of these layouts prints four digits of nineteen by deliberate design. The
comparison is therefore on `account_number_last4` and `routing_number_last4`.
Recorded plainly because it is a weaker check than D-013 implied, and the
honest name for it is tail-to-tail matching of whatever both sides happen to
carry — the invoice may print an IBAN tail while the column holds a domestic
account tail. For a German IBAN those coincide. In general they are not the same
field, and that gap predates this work.

**A state, not a boolean.** `InvoiceFacts.bank_comparison` is a required
`BankComparison` with no default. The interesting states are the ones that are
neither matched nor changed, and a `bool` collapses all of them into `False` —
which reads as *checked and fine*. That is precisely the shape D-013 was written
against, so replacing one defaulted flag with another would have reproduced the
bug in a new place.

**Both identifiers must read, or nothing is claimed.** A routing-only match is
agreement about which *institution* is being paid while never having seen the
destination account — the most dangerous available false negative. All four
layouts print both, so the strict rule costs nothing on the real corpus.

**A routing-only difference is a change.** The tempting softer rule — treat it
as informational because bank mergers and correspondent routes exist — was
rejected. It converts the flagship control into a fail-open path on an unmeasured
guess about fraud shapes, which is exactly what D-014 forbids. And the tolerance
philosophy does not transfer: "immaterial" there means eleven cents of
reconcilable rounding, whereas there is no immaterial fraction of an account
identifier.

**Retired is distinguished from unknown.** An invoice quoting an account this
vendor has *replaced* is a stale or replayed document; one quoting an account we
have never held is a redirect attempt. Same code, same severity, same desk, same
clock — a different first phone call. It costs one enum member and uses the
effective-dating the seed already keeps for exactly this question.

**Four ways to decline, all recorded.** `NOT_EXTRACTED` (the reading predates
this feature — re-extract), `NOT_READ` (nothing legible enough to accuse anyone),
`NO_VENDOR`, and `NO_HISTORY` each write a distinct sentence into
`skipped_checks`. No new taxonomy code. `NO_HISTORY` is live on the seed today
and is the shape of every genuine first invoice from a new supplier; firing the
highest-severity code on that would be a false positive by construction.

The schema field is `ExtractedField[str] | None = None` rather than a default
not-found instance, and that distinction is the whole reason `NOT_EXTRACTED`
exists. `Extraction.fields` is a JSONB blob written once, so rows persisted
before this feature have no such key. A default instance would make "never
asked" byte-identical to "asked and found nothing" — two different facts with
two different remediations. `_empty_result` therefore passes the not-found
instances *explicitly*: a document with no text layer genuinely was asked.

**Not decision-relevant.** The fields are deliberately absent from
`decision_relevant_fields()`. That gate is a strict minimum and an unreadable
payment line scores 0.0, so every invoice without a printed bank block would
trip `LOW_CONFIDENCE_FIELD`. The signal has its own first-class path through the
exception layer, and `lowest_decision_confidence` is additionally a denormalised
column written once at ingest — recomputing it under a new definition would
leave historical rows disagreeing with current ones forever.

**The read floor is a module constant, not a policy knob.** A tolerance answers
"how much drift is acceptable"; this answers "did we read it at all", which is
identity rather than degree. It is also a property of one parser's scoring tiers,
so stamping it into the versioned `TolerancePolicy` would dress a second guess as
calibration at the exact milestone D-014 says not to.

**The unpaid cost, stated plainly.** An invoice whose payment block this parser
cannot read auto-approves with a recorded skip. An escalation on unreadability
was considered and rejected on a technical ground rather than a philosophical
one: its trigger would be computed from the same label list whose failure
produces it, so an unlisted label — `Beneficiary account`, `Empfängerkonto` —
yields "no payment block present" and therefore silence. That is a correlated
detector on the shortest clock in the system. The real fix is parser coverage,
and eventually a full-detail comparison at payment-file generation where the
complete account number exists.

One consequence worth knowing: because the clean path now genuinely compares,
`smoke.sh`'s "no check was skipped" and the integration suite's
`skipped_checks == []` became stronger assertions. A parser regression on the
continental layout presents as a smoke failure about skipped checks, which is one
indirection away from obvious.

Two things fixed in passing. The API was returning the whole extraction blob, so
the payment digits would have been served to any caller; `redacted_fields` drops
them — dropped, not masked, because four digits is a short candidate list. And
`GET /v1/documents/{id}` is the first consumer of that projection.

---

## D-015b — The golden fixture had been quoting a bank account that never existed

**Date:** 2026-09-10 · **Status:** accepted

`build_spec()` printed account `8291` — Northgate's current account — paired with
routing `0248`, the routing code Northgate *retired* forty days before
`SEED_EPOCH`. Each value was individually plausible; the pair existed on no row
in the database.

It had been wrong since M1 and was invisible for exactly as long as nothing read
it. The moment payment-detail comparison went live it turned the golden "clean"
invoice into the highest-severity exception in the system and collapsed every
demo scenario to blocked.

The fixture was corrected rather than the seed. The seed's bank rows are keyed on
a fingerprint that includes `effective_from`, and inserts are
`on_conflict_do_nothing`, so editing a digit and re-seeding a warm database
*inserts* rather than replaces — and for the open row that produces two rows both
claiming to be current, which is the one ambiguity the comparison has to tolerate.

`tests/unit/test_seed_coupling.py` now asserts the coupling directly: the
fixture's payment details must equal the last four digits of the vendor's
currently-effective row, its cited purchase order must exist, and every line it
bills must have a matching SKU and agreed price. No database needed — both sides
are Python literals — so it runs on the `make verify` side of the split.

This is the same lesson as D-015, one layer out. A value that nothing reads is
not validated by being written down, and the comment that documented the coupling
was not a test.

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

**Date:** 2026-09-10 · **Status:** CLOSED by D-016 on 2026-09-10

> Kept rather than deleted. The gap it names is real history, and having
> named it before closing it is worth more than a tidy log. Note that its
> proposed mechanism — comparing the stored HMAC fingerprint — turned out not
> to be executable: the fingerprint covers the full account number and an
> invoice prints four digits of it. D-016 explains what replaced it.

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
