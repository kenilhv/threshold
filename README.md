# Threshold

**An accounts-payable control plane.** It takes an invoice from arrival to
payment without a human touching it — unless it cannot prove it should.

The name is the argument. The system's entire job is to hold a defensible line
between what a machine may execute alone and what a human must decide, and to
show where that line is and why it sits there.

> **Status: M8 of ten.** An invoice goes the whole way: it arrives, it is read,
> it is matched against its purchase order and the goods received, and then one
> of three things happens. It clears every check and the system records its own
> approval. Or something disagrees and it waits in a queue for the person who
> owns that particular problem. Either way, once approved it is paid — once,
> idempotently — and the money is written into a double-entry ledger Postgres
> will not let you unbalance.
>
> Sprints M0 through M8 are done, and M9 all but its last piece: every route
> now knows who is asking; the application no longer connects to Postgres as a
> superuser, which it had since the first commit; the API and the worker hold a
> role each, so the process that parses documents strangers sent cannot record
> that a person approved one; and the columns that name a person or a company
> are withheld from the agent. The written threat model remains. M10 (Terraform,
> load test) is not started.
>
> **To see it working**, in order:
>
> ```bash
> make up          # the whole stack, waits until each part is actually serving
> make demo-match  # four invoices, four different outcomes, with the reason for each
> make smoke       # 41 checks against the running system, end to end
> ```
>
> Then open <http://localhost:3000> and sign in as `buyer` / `buyer`. The front
> page explains the system before asking you to log in; the queue is what a
> reviewer actually works. `make demo-match`'s blocked invoices are mostly price
> disputes, which is why `buyer` is the account to start with.
>
> Every claim in this README is checkable against the repository. Where a number
> appears it was measured, and where something does not exist it says so.

---

## Run it

```bash
make up
```

That is the whole thing. It builds the images, starts Postgres, MinIO and
Keycloak, waits for each to pass its own healthcheck, applies the migrations,
loads reference data, then starts the API, the worker and the console.

| | |
|---|---|
| API docs | <http://localhost:8000/docs> |
| Readiness | <http://localhost:8000/readyz> |
| MinIO console | <http://localhost:9001> — `minioadmin` / `minioadmin` |
| Keycloak | <http://localhost:8080> — `admin` / `admin` |
| Dashboards | <http://localhost:3001> — Grafana, no login. Decisions, queue, API. |
| Traces | <http://localhost:3200> — Tempo. One invoice is one trace across both services. |
| Metrics | <http://localhost:9090> — Prometheus, receiving OTLP rather than scraping. Nine alert rules, each proved to fire and proved to stay quiet. |
| Runbooks | [docs/runbooks](docs/runbooks) — what to do when one of them pages. `make demo-alert` makes one fire. |
| **Console** | <http://localhost:3000> — sign in as `clerk`, `buyer`, `receiving`, `treasury`, `controller` or `auditor` |

The seeded users' passwords are their usernames. That is a local fixture and
would be a finding anywhere else; the realm is committed so the roles are
reproducible, not so the passwords are.

Then prove it actually works rather than merely started:

```bash
make smoke
```

It trusts nothing `docker compose ps` says, and prints its own check count: the audit trigger is
asked to allow an UPDATE and must refuse, a real PDF is ingested twice and must
produce one document, and the queue is inspected for jobs holding an expired
lease.

To see the decision layer make four different calls on four invoices:

```bash
make demo-match
```

Then open the console as `clerk` and as `buyer`. The clerk sees the over-billed
invoice and is told it belongs to the buyer; the buyer sees the same invoice with
a decision form. To check the same thing without a browser:

```bash
make check-console
```

It drives the console as both, including a decision submitted with JavaScript
disabled, and confirms no restricted value reaches the page.

To watch the durability claim hold under an actual crash:

```bash
make demo-crash
```

It `SIGKILL`s the worker container while it holds a lease — no graceful
shutdown, no cleanup — then shows the reaper returning the job with its attempt
history intact.

### How well does it read an invoice?

```bash
make eval
```

Renders four hundred invoices across four layouts, runs the real extractor over
them, and scores every reading against the spec that produced the document. It
prints two calibration curves — field readings as a diagnostic, whole documents
as the thing the auto-approval gate is derived from — and re-derives the gate
from what it finds. No stack required; it takes about fifteen seconds.

The number that comes out of it is in `evals/baseline.txt`, which `make verify`
compares against on every commit. The line worth reading first is
`shipped.wrong`: invoices this system would auto-approve on a misreading. It is
zero, and it is the one number in the file that may only ever fall.

`make test-integration` runs against the live stack. The queue tests provision
and migrate their own database (`threshold_queue_tests`) rather than sharing one
with the running worker, which is a real competing consumer — see D-012.

Other useful targets: `make logs`, `make ps`, `make queue-stats`, `make dlq`,
`make replay id=<uuid>`, `make reap`, `make down`, `make clean` (destroys
volumes), `make migrate-roundtrip`, `make eval-check`, `make verify`.

Requires Docker with Compose v2. No cloud account, no API keys, no paid
services.

---

## The design in one page

An invoice arrives, is extracted by a model that reports per-field confidence,
is matched against its purchase order and goods receipt by **deterministic
rules**, and is either cleared with no exceptions raised or turned into
**typed exceptions, each with a named owner and a deadline**, for review.

Three decisions carry most of the weight, and each is a boundary rather than a
feature:

**The decision layer is deterministic on purpose.** Classification, tolerance
arithmetic, owner routing and state transitions are pure functions with
table-driven tests. Model inference is confined to reading documents. The reason
is one sentence: *a rule's correctness is checkable, a model's is only
estimable, and in finance the difference between those two words is an audit
finding.*

**The agent has no write at all.** It runs on exceptions only and holds four
read tools, each handed a session opened `SET TRANSACTION READ ONLY` — so a
write fails at Postgres, not at a guardrail, and the integration suite proves it
by trying. `escalate` is not a tool; it is how the loop ends. There is no code
path from a model output to a payment.

**A successful injection can reorder attention and nothing else.** Exceptions
carry their owner and deadline from the moment the control fired, so every
invoice is on a named desk with a clock on it before any agent runs. A packet
may only cite codes actually recorded against that invoice, and may only name an
owner one of those *cited* codes already routes to. The red-team suite found
that rule missing — bounding the owner to "one of the four roles" bounds the
alphabet, not the choice — and the attacks that still get through carry a test
each asserting what bounds them, rather than a paragraph saying they are
handled.

**Restricted data never enters a model's context.** Bank details and tax IDs are
classified on the column and dropped from the agent's projection rather than
hashed, because a hash of a bank number is still a bank number to anyone holding
a candidate list. So are the columns that name somebody: the vendor, the buyer's
email address, the filename the supplier chose. The agent sees that a
`bank_details_changed` exception fired, never the account. It cannot leak what
it never held — and the filename it no longer sees was a string an attacker
picked, so that is one injection channel closed rather than defended.

Invoice numbers, amounts and the supplier's own line descriptions *do* reach it,
inside the free text of an exception. They are the evidence it reasons about,
and `tests/unit/test_agent_red_team.py` treats that text as hostile.

**Money moves through a ledger Postgres will not let you unbalance.** Every
payment is a set of postings that sum to zero, checked at `COMMIT` by a deferred
constraint trigger rather than by a service method somebody has to remember to
call. The trigger fires on `UPDATE` and `DELETE` as well as `INSERT`, because
removing a good entry from a transaction that already balanced is the quiet
attack on a ledger. `/v1/ledger/trial-balance` serves the one number that goes
non-zero the moment anything anywhere has gone wrong, and the smoke test asserts
it against the running stack.

**A payment is committed as `submitting` before the rail is called.** That state
is the whole design: a rail that does not answer may still have taken the money,
so writing `failed` on a timeout is how a supplier gets paid twice. Instead the
row stays where it is and the next attempt sends the *same* idempotency key,
derived from the approval decision that authorised it — which is also what the
payment row is unique on, so three mechanisms say the same thing in three places
and can catch each other failing.

The default action of the whole system is to do nothing and ask a human.
Automation is opt-in per rule.

Full reasoning, including what is deliberately *not* distributed and why:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Where things are

```
src/threshold/
  domain/       the decisions, as pure functions — no database, no clock, no model
  services/     the same decisions, once they need a database
  adapters/     the outside world behind ports: extraction, queue, object store,
                payment rail, triage agent — each with a tier that needs nothing
  api/          FastAPI routes
  workers/      the job runtime and one handler per job kind
  observability/ the metric registry every dashboard and alert is checked against
console/        the operator console (Next.js, back-end for front-end)
evals/          the extraction evaluation harness and its committed baseline
migrations/     Alembic, 8 revisions, each reversible and proved so
infra/local/    compose, the Keycloak realm, Prometheus rules, the Grafana dashboard
docs/           ARCHITECTURE.md, DECISIONS.md (34 records), 7 runbooks
tests/unit/     690 tests, no stack required
tests/integration/  161 tests, requires `make up`
```

The split that matters is `domain/` against everything else. A rule in `domain/`
takes values and returns values, so every case it handles is a row in a table
test. The moment a decision needs a database it moves to `services/`, and the
reason is stated where it happens rather than implied.

## Built so far

- [x] **M0 — one command.** Compose stack with real healthchecks and ordered
      startup, reversible Alembic migrations, an append-only audit log enforced
      by a database trigger, `NUMERIC` money, PII classification declared on
      every column, structured logging with redaction, OIDC realm as committed
      configuration, liveness separated from readiness.
- [x] **M1 — ingestion and extraction.** `POST /v1/documents` accepts a PDF,
      content-addresses it, stores it in S3-compatible storage and persists an
      immutable versioned extraction with per-field confidence. Idempotent at
      two independent layers. A deterministic extraction tier runs with no model
      weights, no API key and no network, so CI exercises the real pipeline. Four
      byte-deterministic fixture layouts carry the shapes that break extractors.
- [x] **M2 — durable job execution.** A Postgres queue behind a narrow `Queue`
      port: `SELECT … FOR UPDATE SKIP LOCKED` for contention-free claims,
      two-minute leases, three attempts, exponential backoff from five seconds,
      and a dead-letter table with a replay path. Jobs are enqueued in the same
      transaction as the state change that caused them, so the two cannot
      disagree. A claim is a state change, never a delete, which is what makes a
      killed worker's job recoverable rather than gone. An integration test runs
      four workers against forty jobs and asserts each was claimed exactly once;
      `make demo-crash` kills a worker holding a lease and shows the work
      return.
- [x] **M3 — the decision layer.** Three-way match between invoice, purchase
      order and goods received, at line level, as pure functions with a
      table-driven test per case. A frozen exception taxonomy whose distinctions
      come from SAP's tolerance keys rather than from first principles — notably
      that billed-before-receipt is a different problem from over-billing, with
      a different owner. Every exception carries a category, an owner and a
      deadline, all written onto the row rather than re-derived later.
      Tolerances are a floor, a percentage and a ceiling, versioned as a set and
      stamped on every decision. Checks that could not run honestly are recorded
      as skipped rather than passing silently. Payment details are compared
      against the vendor's own bank history, so a genuine invoice resent with
      altered remittance details blocks to treasury even when every number on it
      agrees.
- [x] **M4 — a person decides.** The API authenticates against Keycloak and
      verifies algorithm, issuer, audience and expiry, with the classic
      algorithm-confusion attack tested. Every state transition writes an
      audit row with the request that caused it. A review projection derived
      from the column declarations drops restricted data, and a test walks
      every model to prove it. Decisions live in one append-only table where
      the machine and the person are peers, and you may decide an invoice only
      if you own every open exception on it. The console is a back-end for
      front-end — the browser never holds a token — and works with JavaScript
      disabled.
- [x] **M5 — observability that can be checked.** One invoice is one trace
      across two processes and two queue hops, and the trace id on every audit
      row resolves in Tempo to the whole story — including the SQL spans, which
      were silently missing until a metrics test asked why a declared metric had
      never been produced. Metrics sit behind a registry the SDK enforces: an
      undeclared label is dropped before export, and the histogram buckets come
      from measured distributions rather than defaults that would have put every
      number this system produces in one bucket. Every metric names the
      dashboard panels and alert rules that read it, and tests assert in both
      directions, because a metric nobody plots is this repository's oldest
      defect wearing a new hat. Readiness checks what the service actually uses
      and deliberately never the identity provider. Nine alert rules, each with
      a test that makes it fire and a test that keeps it quiet, and six runbooks
      that are checked to exist.
- [x] **M6 — measuring it instead of asserting it.** Four hundred synthetic
      invoices whose answer key is the spec that rendered them, so ground truth
      cannot drift from the document. The auto-approval gate is no longer a
      guess: it was 0.50 and provisional for three milestones, and measurement
      showed it admitted a bucket of invoices that were *always* misread. It is
      now 0.60, derived from the pooled accuracy of everything a gate would
      accept, bounded by a Wilson interval rather than a point estimate, and
      pinned by a test to the interval the evidence supports. The confidence
      signal is shown to rank well (AUC 0.99) and calibrate badly (ECE 0.19) —
      an ordinal score wearing probability clothing — and the gate is derived
      in a way that does not need it to be a probability. `make verify`
      compares every count against a committed baseline, exactly, and the
      harness refuses to report at all if the corpus shrank or stopped
      producing errors to calibrate against.
- [x] **M7 — the triage agent, and a red team that found the boundary wrong.**
      The agent has four read tools, each handed a session opened
      `SET TRANSACTION READ ONLY`, so a write fails at Postgres rather than at a
      guardrail — proved by trying, including through raw SQL so the guarantee
      is shown not to live in the ORM. `escalate` is not a tool; it is how the
      loop ends. The red-team suite was written to test that boundary and found
      it insufficient: bounding the escalation's owner to "one of the four
      roles" bounds the alphabet and not the choice, and a payment redirect
      addressed to an accounts-payable clerk needs no illegal characters. A
      packet may now only cite codes actually recorded against that invoice and
      only name an owner one of those *cited* codes already routes to, so a
      completely captured model can reorder the priority of people already
      involved and do nothing else. Thirteen attacks, nine refused and four that
      get through carrying a test each for what bounds them. Every property was
      confirmed by deleting it and watching the suite die.
- [x] **M8 — money that cannot quietly stop adding up.** Double-entry, with the
      balance rule enforced by a deferred constraint trigger at `COMMIT` rather
      than by a service method somebody has to remember to call — and firing on
      `UPDATE` and `DELETE` too, because removing a good entry from a
      transaction that already balanced is the quiet attack on a ledger. The
      first version of that trigger had a hole, found by checking a fifth case
      after four had passed, and 0008 is the fix. A payment is committed as
      `submitting` *before* the rail is called, because a rail that does not
      answer may still have taken the money; every retry sends the same
      idempotency key, derived from the approval decision the payment row is
      also unique on. Building it turned up that the schema could say an invoice
      was blocked and could not say for how much.
- [ ] **M9 — least privilege, row-level security, withheld identities, written
      threat model.** Three of four increments done.
      *Done:* the two document reads that had answered anybody since M1 now
      require a token, and a test walks every route so the next one cannot
      forget. No process connects as a superuser or an owner any more; the
      runtime roles hold table-by-table grants, the record is `SELECT`+`INSERT`,
      reference data is `SELECT`, and `UPDATE` exists only on the columns that
      say how far along a job or a payment is. That last narrowing came from attacking the
      first version: with table-wide `UPDATE ON payment`, a review running as the
      restricted role pointed a pending payment at another vendor's bank account
      and multiplied the amount by five. Postgres now also enforces the payment
      state machine and refuses a payment row that disagrees with the
      authorisation it cites. Reading the executor for that work found it would
      have paid a *rejected* invoice if handed the decision's id; it no longer
      does. D-034 has the grants, the findings, and -- stated as plainly -- what
      a compromised application can still do.
      The API and the worker then stopped sharing an identity: a role each,
      and rules Postgres enforces about which may write what. The worker writes
      the record — extractions, matches, payments, the ledger — and may take the
      one decision a machine is allowed, `auto_approved`, and only on an invoice
      the match cleared. The API records what people decide and cannot write the
      evidence its own decision would be about; it updates nothing, anywhere.
      `authorise()` now also exists as a Postgres function, so a future route
      that inserts a decision without asking is refused by the database. Reads
      are deliberately *not* filtered, and D-035 explains why doing so would be
      an authorisation bug rather than a privacy control.
      Then the columns that name somebody — a vendor's name, a buyer's email
      address, the filename a supplier chose — became a declared property of
      the column, and the agent's projection drops them. That replaces the
      "stable-alias scheme" this roadmap promised for two milestones: it was
      designed, written down, attacked and abandoned, and D-036 is the argument.
      The short version is that the outside reviewer it was for does not exist,
      that for the agent an alias carries exactly what dropping carries, and
      that a shared key over a list of vendor names is a liability rather than a
      control. Closing the filename channel also cost the red-team suite one of
      its three attack surfaces, and exposed that its tripwire had been
      recomputing the projection's rule instead of asking it.
      *Not done:* the written threat model. The classification layer the rest
      builds on exists and is load bearing: every column declares a
      sensitivity, an undeclared one is treated as restricted, and the review
      projection is derived from the mapper rather than from an allowlist. What
      is missing is the threat model written down where it can be argued
      with — the one deliverable of this milestone that is prose rather than
      code, and the one that decides whether any of the above was aimed at the
      right thing.
- [ ] **M10 — Terraform, load test, measured numbers.** Not started, and the
      most deferrable: it is the milestone that describes deployment rather than
      behaviour. The performance numbers this system does have were measured
      when they mattered — histogram buckets from real distributions, the
      constraint trigger's 0.285 ms, the alert's detection latency timed with
      `make demo-alert` — rather than collected at the end as an exercise.

---

## Running it in a cloud

Nothing here is written to be local-only. The differences are two:

| Local | AWS |
|---|---|
| MinIO | S3 — same `boto3` calls, different endpoint |
| Postgres container | RDS Postgres |
| `job` table + `SKIP LOCKED` | SQS with a visibility timeout and redrive policy |
| Compose services | ECS Fargate tasks behind an ALB |
| Env files | Secrets Manager |
| Keycloak | Cognito, or an existing IdP |

The queue driver and the secret source are the only two places the code paths
genuinely differ, and both sit behind an interface. Terraform describing the AWS
shape is validated in CI from M10 and deliberately never applied — spending
money to host a demo proves less than the interface that makes it a
configuration change.
