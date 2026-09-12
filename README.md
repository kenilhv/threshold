# Threshold

**An accounts-payable control plane.** It takes an invoice from arrival to
payment without a human touching it — unless it cannot prove it should.

The name is the argument. The system's entire job is to hold a defensible line
between what a machine may execute alone and what a human must decide, and to
show where that line is and why it sits there.

> **Status: M4.** A PDF ingests, a worker extracts it and matches it against
> its purchase order and the goods received, and then one of two things
> happens. Either nothing went wrong and the system records its own approval,
> or the invoice waits in a queue for the person who owns what did — and
> anyone else who opens it sees the reason in place of the button.
>
> `make demo-match` submits four invoices: one approves itself, one blocks on
> price and goes to the buyer, one goes to receiving because nothing has
> arrived, and one, arithmetically perfect, blocks to treasury because it asks
> to be paid into an account this vendor has never used. The console at
> <http://localhost:3000> is where those three get decided.
>
> The milestones below are honest about what does not exist yet. Every claim
> in this README is checkable against the repository.

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
| Metrics | <http://localhost:9090> — Prometheus, receiving OTLP rather than scraping. |
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

`make test-integration` runs against the live stack. The queue tests provision
and migrate their own database (`threshold_queue_tests`) rather than sharing one
with the running worker, which is a real competing consumer — see D-012.

Other useful targets: `make logs`, `make ps`, `make queue-stats`, `make dlq`,
`make replay id=<uuid>`, `make reap`, `make down`, `make clean` (destroys
volumes), `make migrate-roundtrip`, `make verify`.

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

**The agent's only write is an escalation packet.** It runs on exceptions only,
holds read tools plus exactly one terminal action, and a test reflects over the
tool registry asserting every other tool is read-only. There is no code path
from a model output to a payment — a structural property, not a filter.

**Restricted data never enters a model's context.** Bank details, tax IDs and
invoice numbers are classified on the column and dropped from the review
projection rather than hashed, because a hash of a bank number is still a bank
number to anyone holding a candidate list. The agent sees `bank_changed: true`,
never the account. It cannot leak what it never held.

The default action of the whole system is to do nothing and ask a human.
Automation is opt-in per rule.

Full reasoning, including what is deliberately *not* distributed and why:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

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
- [ ] M5 — OpenTelemetry end to end, dashboards, alert rules, runbooks
- [ ] M6 — evaluation harness, calibration curve, CI regression gate
- [ ] M7 — the triage agent and its injection red-team suite
- [ ] M8 — idempotent payments and a double-entry ledger
- [ ] M9 — row-level security, pseudonymisation, written threat model
- [ ] M10 — Terraform, load test, measured numbers

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
