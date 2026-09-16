# Architecture

An accounts-payable control plane. Its job is to take an invoice from arrival to
payment without a human touching it — unless it cannot prove it should.

Everything below that is not yet built is marked. Boxes in this document
correspond to directories in the repository; there are no aspirational
components.

---

## The shape

```
upload / email intake
   │  Idempotency-Key, sha256 content hash
   ▼
Ingest API (FastAPI) ──► object store (MinIO, S3 API) ──► documents row
   │  enqueue in the SAME transaction as the state change
   ▼
job queue  (Postgres, SELECT … FOR UPDATE SKIP LOCKED, leases, attempts, DLQ)
   ▼
Worker pool (Python, asyncio)
   ├─ extract      VLM → schema-validated fields + per-field confidence  [MODEL]
   ├─ normalize    currency, dates, vendor resolution                    [DETERMINISTIC]
   ├─ match        3-way PO ↔ receipt ↔ invoice, line level              [DETERMINISTIC]
   ├─ classify     frozen exception taxonomy, tolerances, owner routing  [DETERMINISTIC]
   └─ triage       agent loop, exceptions only, read tools + escalate    [AGENT]
   ▼
Decisions — append-only, machine and human in one table, one terminal row
   ▼
Operator console (Next.js, BFF) ── the owner approves / rejects / holds
   ▼
Payment executor (Go) — idempotency key, double-entry ledger, Stripe sandbox
```

Built today: everything down to and including the console. A PDF ingests, a
worker extracts it, a second job matches it against its purchase order and the
goods received, and either the system records its own approval or the invoice
waits in a queue for the person who owns what went wrong. An invoice that failed
gets a third job, which asks the triage agent which desk should look first.

Both agentic steps run their deterministic tier. Neither model tier exists yet,
and both boundaries do — which is the order this project builds in, argued in
D-029. The payment executor is M8.

The diagram used to say "state machine" for the approval step. There is no
state machine, and that is a decision rather than an omission: the current state
of an invoice is derived from an append-only table of decisions, so there is no
status column that can disagree with its own history.

---

## Three boundaries that carry the design

### The decision layer is deterministic on purpose

Classification, tolerance arithmetic, owner routing and state transitions are
pure functions with table-driven tests. Model inference is confined to reading
documents.

*A rule's correctness is checkable; a model's is only estimable, and in finance
the difference between those two words is an audit finding.*

This also makes the eval story honest. Deterministic classification is expected
to score 100% against labelled dispositions — any miss is a bug, not a model
failure — so the extraction and triage layers can be measured on their own axes
without a single blended number hiding which part regressed.

### The agent has no write, and `escalate` is not a tool

The triage agent gets four read tools. `escalate` is not among them, and that is
stronger than guarding it would be: it is not a tool at all, it is how the loop
ends. The agent returns an `EscalationPacket` and `services/triage.py` decides
what to do with it, exactly as it would with a form submitted by an untrusted
client — because that is what it is.

Every tool receives a session opened `SET TRANSACTION READ ONLY`, so a write
through one fails at the database regardless of what the calling code believed.
`tests/integration/test_agent_boundary.py` proves it by trying — including
through raw `text()`, so the guarantee is shown not to live in the ORM.

There is no code path from a model output to a payment. That is a structural
property rather than a filter, and structural properties survive questioning in
a way that "we added a guardrail" does not.

**The agent is also not the router, which is what bounds a successful
injection.** Exceptions carry `owner_role` and `due_at` stamped when the control
fired, so every invoice is on a named desk with a clock on it before any agent
runs. A packet may only cite codes actually recorded against that match result,
and may only name an owner that one of the *cited* codes already routes to. A
captured model can therefore reorder the priority of people already involved and
write them a sentence. It cannot unassign anything, stop a clock, or approve.

That rule exists because the red-team suite found the first version wrong:
bounding `owner` to "one of the four roles" bounds the alphabet and not the
choice, and a payment redirect addressed to an accounts-payable clerk needs no
illegal characters. D-031 records the sequence, because a suite that only
confirms the defences you already believed in is a suite you chose.

The residual risk is named rather than hidden. The summary is free text written
by a model that read the supplier's own line descriptions — `matching.py`
interpolates them into `match_exception.detail` — and no validation escapes
natural language. It is capped, held in `AgentText` so an f-string cannot leak
it, and rendered by the console quoted and attributed to the tier that wrote it.
Within that it can still persuade the person who reads it.
`TestTheAttacksThatSurvive` asserts each bound rather than claiming the risk is
gone.

### Restricted data never enters a model's context

Every column declares a sensitivity — `public`, `internal`, or `restricted` —
in `domain/classification.py`, and an undeclared column is treated as restricted
so that forgetting to classify one fails closed.

Bank details, tax IDs and invoice numbers are dropped from the review
projection, not hashed. A hash of a bank account is still a bank account to
anyone holding a candidate list, and hashing the vendor name destroys the
reviewer's task at the same time — nobody can judge whether an invoice looks
wrong when the vendor reads `a3f9c1`.

Pseudonymisation instead preserves the shape of the judgment: stable HMAC
aliases so payment history still reads as history, and a single scaling factor
per dataset so a 54% overbill still reads as 54%.

The consequence for the agent is that it *cannot* exfiltrate a bank number
because it never held one. It sees `bank_changed: true`. Privacy control and
injection control, one mechanism.

---

## The decision, and why it is arithmetic rather than judgment

Three-way match asks one question: does what we are being billed agree with what
we agreed to buy and what actually arrived? All three legs are rows in Postgres,
so the answer is a pure function and `domain/matching.py` is written as one — no
database, no clock, no model. `tests/unit/test_matching.py` states every case as
inputs and an exact set of expected exception codes, which is what makes "why
was this blocked?" answerable by reading a function instead of replaying a run.

**The taxonomy is frozen, and its distinctions are borrowed.** Codes are added,
never repurposed: dashboards, SLA reports and eventually the labelled evaluation
set all key off those strings, so redefining one silently reinterprets every
decision already stored under it. The cuts come from SAP's Logistics Invoice
Verification tolerance keys rather than from first principles, because those
survived decades of real accounts-payable practice. Three are easy to miss:

* Billed-before-receipt (DW) is not over-billing (DQ). The arithmetic is
  identical — billed exceeds received — and the problems are different. Over-
  billing is a dispute with the vendor and belongs to the buyer; nothing
  received yet is usually a paperwork lag and belongs to receiving. Collapsing
  them sends half of everything to the wrong desk.
* Immaterial differences are absorbed rather than escalated (BD). Paging a human
  for eleven cents costs more than the money and teaches reviewers to approve
  without reading, which is how the expensive exception gets waved through too.
* Under-billing is informational and never blocks. Being charged less than
  agreed is not a control failure.

**Every exception carries a category, an owner and a clock.** Routing follows
where the authority to fix the problem sits: accounts payable cannot decide
whether a price rise was agreed, and a buyer cannot confirm a pallet arrived.
The owner and the deadline are written onto the row rather than derived from the
taxonomy at read time, because a report that re-derives ownership from today's
rules silently restates who was responsible last quarter, and an SLA measured
from `now()` is a deadline that never arrives.

**A tolerance is three numbers, not one.** A floor below which any difference is
immaterial, a percentage for the middle of the range, and a ceiling so a tenth
of a percent of a very large invoice cannot be absorbed in silence. A single
percentage fails on ten-cent parts; a single absolute fails on million-dollar
orders. The floor gates, it does not trigger. The whole policy is versioned as a
set and stamped on every decision, because the thresholds are not independent
and a decision that cannot name the ones it used cannot be reproduced.

**The disposition is derived, never decided.** It is the worst severity present
in the exception set. A separate rule saying "block this one" would be a second
source of truth, and the drift would eventually show up as an invoice reporting
a blocking exception and paying anyway.

**Payment details are compared against the vendor's own history.** A vendor
bank change is the shape almost every real accounts-payable fraud takes: a
genuine invoice, resent with altered remittance details. So the last four digits
of the account and routing code are read off the page and compared against the
row currently in force for that vendor — and an invoice that is otherwise
perfect, right order, right price, goods received, blocks to treasury on the
shortest clock in the system.

The comparison reports a state rather than a boolean, because the interesting
answers are the ones that are neither *matched* nor *changed*: the reading
predates the feature, nothing legible was found, no vendor resolved, or the
vendor has no payment details on file at all. A boolean collapses every one of
those into "false", which reads as checked and fine. Each instead writes its own
sentence into `skipped_checks`. An account the vendor has *replaced* is
distinguished from one never seen — same code and same desk, different first
phone call.

The honest limit: this is tail-to-tail matching of four digits, not scheme-aware
account verification, and an invoice whose payment block the parser cannot read
auto-approves with a recorded skip. Both are stated in D-016 rather than papered
over.

**A check that did not run is recorded, not omitted.** Cross-currency prices are
not compared at all — 100 EUR against 100 USD yields a variance of zero, which
is not a match but a category error — and an extraction carrying no line items
compares nothing. Both write into `skipped_checks` on the result, because an
absent check and a passing check are indistinguishable from the outside, and
that difference is the whole value of the answer.

Two properties in the test suite keep the taxonomy honest: every code must be
reachable from the case table, and every owner role must receive work. Both
failed the first time they ran. `TAX_VARIANCE` was in the taxonomy with nothing
able to raise it, because checking tax needs per-jurisdiction rates this system
does not have — so it was removed rather than left looking covered.

`make demo-match` submits four real invoices and prints what happened to each.

---

## Deciding, and who may

A decision is a row in `approval_decision`, and the machine and the person are
peers in it. When a match raises nothing, the matcher writes `auto_approved`
with `actor_type = 'system'`; when a person decides, the row differs in its
actor and its reason and nothing else. "What did this system decide without
asking anybody, and who decided the rest" is therefore one query, not an
inference from the absence of a human row.

**You may decide an invoice only if you own every open exception on it.** Not
"the owner of the worst exception", which looks equivalent and is not.
Exceptions sort by severity and then by code, so among blocking codes the
alphabet breaks the tie — and `arithmetic_mismatch`, owned by a clerk, sorts
ahead of `bank_details_changed`, owned by treasury. An invoice with an altered
total *and* redirected remittance details is the likeliest composite fraud in
this domain, and a worst-owner rule hands it to the clerk. Under the real rule
it needs both roles, so in M4 it stays blocked and says which roles it needs.
There is no multi-party approval yet; the limitation is stated, and it fails
safe.

**A decision belongs to a body of evidence, not to a document.** It references
one `match_result` version — this extraction, judged under this policy. When a
receipt arrives and the matcher writes a new version, the old approval simply
does not refer to it. Nothing has to be invalidated because nothing was ever
attached to the document in general.

**Only approve and reject are terminal**, enforced by a partial unique index
rather than a read-then-write. `held` is a state somebody is in the middle of.
`auto_approved` is deliberately not terminal either: D-016 records that an
invoice whose payment block cannot be read auto-approves with a recorded skip,
and freezing that would make the project's worst documented gap unreachable by
a person.

Three refusals, and none of them is a retry. A `403` names the roles the
invoice needs. A `409` for evidence that moved carries the version seen and the
version that exists, because the right next step is to look again. A `409` for
a second terminal decision says who decided and what. Every human decision
requires a reason, and `overrode` records which codes it cleared, so "approved
over a bank-detail change" is a number you can count.

## The console, and where the token lives

The console is a back-end for front-end. It performs authorization code with
PKCE against Keycloak by hand, keeps the access token in an httpOnly cookie on
its own origin, and calls the API from the server. The browser never holds a
bearer token.

That is a choice with a cost — an extra hop on every page — and the reason is
specific to this application. It authorises payment, and it renders text that
came from vendors. A single cross-site scripting bug in a public-client console
would hand an attacker a token for an API that approves money; here, the worst
an injected script can do is act as the user through this origin, which the
same-site cookie and the audit trail both constrain.

Two issuers, not one. The browser is sent to `localhost:8080`; the container
exchanges the code at `keycloak:8080`. Keycloak is pinned with `KC_HOSTNAME` so
the token's `iss` is stable, and the API fetches keys from a separately
configured JWKS URL. Deriving either from the other is what makes a correct,
exact issuer check reject every real token.

The queue, the invoice and the decision are server-rendered. There is exactly
one client component — the decision form — and it is a client component for one
reason: the outcome of a submission has to be announced in a live region that
survives the response. There is no optimistic UI, because the server may refuse
and a screen that shows "approved" and takes it back is the one interaction this
product must not have. The form also works with JavaScript disabled, which
`scripts/check_console.py` exercises end to end.

What is on screen is the smallest set that lets an owner decide: the exceptions,
their owner and deadline, the checks that did not run at the same weight as the
ones that failed, and the history. What is not: a dashboard, charts, or any
number that cannot be computed from anything but seed data. For somebody who may
not decide, the form is replaced by the reason, in domain terms.

## How work survives a crash

The queue is the part of this system most likely to be built badly, so it is
worth being explicit about what it does.

A worker claims a job by **updating** it, never by deleting it:

```sql
SELECT id FROM job
 WHERE state = 'pending' AND run_after <= now()
 ORDER BY run_after, id
 LIMIT :n
   FOR UPDATE SKIP LOCKED
```

then sets `state = 'claimed'`, `claimed_by`, `lease_expires_at = now() + 2min`
and `attempts = attempts + 1`, all in one transaction.

Two properties fall out of that shape:

**The pool scales without coordinating.** `SKIP LOCKED` lets a worker step over
a row another worker has locked instead of blocking behind it, so four workers
draining one queue do not serialise on its head. `FOR UPDATE` alone passes every
single-worker test and then, depending on isolation level, either serialises the
pool or hands one row to two workers. In accounts payable that second failure
mode means paying an invoice twice, so it is tested directly: four concurrent
workers, forty jobs, each claimed exactly once.

**A lease is measured on one clock.** Every timestamp the queue writes and
later compares comes from the database's `now()`, never the worker's. The reaper
judges a lease against the database clock, so a lease computed on the worker's
would only be as good as the agreement between the two — and a worker VM that
drifts behind after the host sleeps would write leases that are already
expired, handing live work to a second worker. See D-019.

**A killed worker loses nothing.** The row is still in the table with its
payload and attempt history; the only thing wrong with it is that it is
`claimed` by a process that no longer exists. A reaper — running under advisory
lock `0x74855170` so exactly one worker sweeps, whatever the pool size — returns
any job whose lease has expired to `pending`. It does not increment `attempts`,
because the claim already counted that try and the job did not fail; the
infrastructure did.

`scripts/demo_crash_recovery.sh` performs this with a real `SIGKILL` against the
real container. Nothing in it is simulated, because a graceful shutdown would
prove nothing about crash recovery.

**The pool outlives the database.** A claim that raises has claimed nothing, so
the loop logs it and backs off — from the poll interval to a thirty-second cap,
reset by the first success — rather than exiting. This is not hypothetical
robustness: running `make migrate-roundtrip` against a live stack dropped the
`job` table underneath a polling worker and killed it permanently, which is the
same code path as a Postgres restart or an RDS failover. `restart:
unless-stopped` on the long-running services is the backstop for an actual
crash; the one-shot migrate and seed jobs keep `restart: "no"`, because a job
that reruns itself is not a job.

**Failures are sorted before they are retried.** The handler raises
`TransientJobError` for a provider blip or a connection reset — back to the queue
with exponential backoff from five seconds, capped at ten minutes, three attempts
including the first. It raises `PermanentJobError` for a corrupt document or a
payload naming a row that no longer exists, which goes straight to the
dead-letter table without burning the budget. Retrying everything is how a single
poison message occupies a worker for an hour.

Nothing is deleted at the end either. A dead letter keeps the original payload
and the error, and `scripts/queuectl.py` replays it once the cause is fixed:

```bash
make dlq                 # what failed, and why
make replay id=<uuid>    # or id=--all
make queue-stats         # depth by state
make reap                # return jobs whose worker died
```

No jitter on the backoff, deliberately. Jitter prevents a thundering herd when
many clients retry against one dependency; here the jobs are already spread by
their own arrival times and the pool is four processes. Adding it would be cargo
cult, and it would make the retry schedule untestable without controlling a
random source.

---

## What is deliberately not distributed

Knowing when not to is the part that is hard to demonstrate, so it is written
down rather than left implicit.

**No Kubernetes.** One `docker compose up`. Accounts payable is hundreds of
invoices per day, not thousands per second; the operational cost of a control
plane exceeds the workload by orders of magnitude. Manifests for a single-node
laptop application would be resume-driven architecture.

**No Kafka or RabbitMQ.** A broker gives at-least-once delivery but makes
"enqueue the job" and "commit the state change" two systems, which is a
distributed transaction. Postgres gives both in one commit via a transactional
outbox and `SELECT … FOR UPDATE SKIP LOCKED`. The honest cost: it does not fan
out, it does not replay a topic, and somewhere above a few hundred jobs per
second the polling loop becomes the bottleneck.

A `Queue` protocol marks where that swap would happen. `PostgresJobQueue` is the
only implementation — there is no SQS adapter in this repository and the
milestone list does not promise one. What the protocol does buy today is honest
and small: producers bind to `enqueue` and workers to the claim/settle cycle, so
no request handler can reach the operational verbs, and `tests/unit/test_ports.py`
type-checks the adapter against the port so a signature cannot drift silently.

It also records the constraint rather than hiding it. Every method on the port
takes the caller's `AsyncSession`, because the enqueue has to commit with the
state change. Anything broker-backed can only satisfy that shape by giving up
the single commit and accepting a real duplicate window — which is a trade to
make deliberately, not to discover after the interface has made it look free.

**No microservices.** Two deployable units: the Python plane and the Go payment
executor. That split has a real reason — the money path wants transactional
integrity, cheap concurrency and a small auditable surface, and it is the one
place a second language pays for itself. Any further split would be a boundary
invented to look distributed.

**No separate vector database.** pgvector in the same Postgres: same
transaction, same backup, same restore drill.

**No event sourcing or CQRS.** The append-only audit table provides the
auditability that motivates event sourcing, without the read-model rebuild
machinery.

**No gRPC, no GraphQL, no service mesh.** Two processes on one host. REST with a
generated OpenAPI contract.

---

## Decisions worth the words

### Migrations are their own unit of work

`migrate` is a separate compose service that runs to completion before `api` and
`worker` start, gated on `condition: service_completed_successfully`.

Running migrations at application startup means two API replicas race to migrate
the same database. It is a real outage and an easy one to avoid.

Every migration is reversible and `make migrate-roundtrip` proves it by running
upgrade → downgrade → upgrade. A migration nobody dares reverse is a migration
that will be reversed by hand at 3am.

### The audit log is append-only in the database, not by convention

`audit_event` has a `BEFORE UPDATE OR DELETE` trigger that raises. The
application has no update or delete path either, but conventions are not
guarantees, and an audit log that can be edited is not an audit log. The smoke
test asserts the trigger actually fires.

Every event carries `trace_id` — the same id as the distributed trace (M5). A
compliance question ("who approved this and what did the system know?") and an
engineering question ("why was this slow?") then resolve to the same row.

### Liveness and readiness are different questions

`/healthz` touches nothing. `/readyz` checks Postgres and the object store with
a 2-second timeout and returns 503 if either is down.

Conflating them causes outages: a liveness probe that checks the database will
restart every healthy replica during a failover, turning a recoverable
dependency blip into a full one.

Readiness deliberately does **not** check the identity provider, and that is a
decision rather than an omission. An IdP is shared, so checking it fails
readiness on every instance of every service at the same instant and leaves a
load balancer nowhere to route — the probe causes a worse outage than the
fault. It would also be false: signing keys are cached, so an instance that has
fetched them keeps validating tokens while Keycloak is unreachable. So identity
is *reported* and never gates routing — how many keys this instance holds and
how long ago it learned them, read from cache without calling Keycloak. Each
dependency carries `required` on the wire, so the response says why an instance
is ready with something in the list not ok. See D-025.

### Money is `NUMERIC(18,4)` and `Decimal` end to end

No float appears in any monetary path. A test asserts a JSON round trip loses no
precision, including the `0.1` case that motivates the rule.

Currency is a separate ISO-4217 column. Arithmetic never crosses currencies
without an explicit rate.

### Identity is delegated, not hand-rolled

Keycloak, OIDC authorization-code with PKCE, realm committed as JSON so roles
are reproducible rather than clicked into existence.

The four roles are domain concepts — `ap_clerk`, `buyer`, `controller`,
`auditor` — because owner routing maps each exception class to exactly one of
them. The authorisation model and the exception taxonomy are the same model.

This is a deliberate correction. Another repository in this portfolio contains a
hand-rolled HS256 implementation with a committed default secret and password
hashing that is `sha256("prefix:" + password)` — one global salt, no KDF, no
work factor. Delegating to an implementation of the specification removes the
class of mistake instead of patching one instance of it.

### The seed is deterministic

Bank-account versions are unique on `(vendor_id, fingerprint, effective_from)`.
Deriving `effective_from` from `now()` made every run insert new rows, so the
seed silently stopped being idempotent and the table grew on each `up`.

The smoke test caught it — 12 rows where 4 were expected. Timestamps now anchor
to a fixed `SEED_EPOCH`, which keeps the data internally consistent while making
re-runs produce identical rows.

### The extractor is measured, not asserted

`make eval` renders four hundred invoices across the four layouts, runs the real
extractor over them, and scores every reading against the spec that produced the
document. The spec *is* the answer key, so there is no labelling step and the
ground truth cannot drift from the page — the one thing a hand-labelled corpus
can never promise.

Two units are measured and the difference between them decides the gate. A
**field reading** answers "when the extractor says 0.58, is that value right",
which is the diagnostic. A **document** answers what the gate actually asks: it
takes a minimum across five fields and approves an invoice, so an invoice with
four right fields and one wrong total is a wrong invoice.

The auto-approval threshold is derived from the second curve rather than chosen.
The rule is stateable: the lowest threshold whose *accepted set* has a Wilson
lower bound on accuracy above the target. Pooled, because a gate selects a set
and the risk belongs to everything it accepts. A lower bound, because a bucket
of two readings reporting "100% correct" is noise — that bucket was 60% correct
at four hundred documents. See D-027.

`evals/baseline.txt` records the result and `make verify` compares against it
exactly, on integer counts. The harness refuses to report at all if the corpus
shrank, lost a layout, read nothing correctly, or read *everything* correctly —
because a corpus with no errors in it calibrates nothing, and every one of those
failures would otherwise surface as an improvement.

**What it cannot tell you.** The corpus is synthetic. Every check defends
against the evaluation being *vacuous*; none defends against it being
*unrepresentative*, and no amount of engineering inside the harness substitutes
for real documents with a human-made answer key. See D-028.

---

## Running this in a cloud

| Local | AWS | Code delta |
|---|---|---|
| MinIO | S3 | endpoint URL |
| Postgres container | RDS Postgres | connection string |
| `job` table + `SKIP LOCKED` | SQS + visibility timeout + redrive | queue driver (behind an interface) |
| Compose services | ECS Fargate behind an ALB | none |
| Env files | Secrets Manager | secret source (behind an interface) |
| Keycloak | Cognito or an existing IdP | issuer URL |

Two places the code paths genuinely differ — the queue driver and the secret
source — and both sit behind an interface with tests.

Terraform describing the AWS shape is validated in CI from M10 and deliberately
never applied. Paying to host a demo proves less than the interface that makes
it a configuration change.

---

## Repository layout

```
src/threshold/
  api/            FastAPI app, routes, request context
  domain/         entities, field classification — no I/O, no framework
  adapters/       database, object store, queue — everything that talks outward
  workers/        the worker process
  observability/  structured logging, traces, and metrics behind a label registry
  config/         twelve-factor settings
migrations/       Alembic, reversible, one transaction per upgrade
infra/local/      Keycloak realm and other local-only fixtures
  services/       use cases that compose adapters — ingestion, matching
scripts/          seed, smoke test, queue CLI, crash and match demos, console check
console/          the operator console (Next.js, App Router, server-rendered)
tests/unit/       no stack required, runs on every commit
tests/integration/ requires `make up`
```

`domain/` depends on nothing. `adapters/` depends on `domain/`. `services/`,
`api/` and `workers/` depend on both, and on the ports rather than the concrete
adapters. Dependencies point inwards, which is what makes the
rules engine testable without a database and the queue swappable without
touching the classifier.
