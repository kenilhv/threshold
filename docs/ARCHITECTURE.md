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
Approval service — state machine, optimistic concurrency, audit events
   ▼
Operator console (Next.js) ── human approves / rejects / holds
   ▼
Payment executor (Go) — idempotency key, double-entry ledger, Stripe sandbox
```

Built today: the API, the schema, the audit log, the object store, the identity
realm, and a worker process that starts and stops cleanly. Everything from the
queue rightwards is M2 onwards.

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

### The agent's only write is an escalation packet

The triage agent (M7) gets read tools plus exactly one terminal action:
`escalate(...)`. A unit test reflects over the tool registry and asserts every
other tool is read-only.

There is no code path from a model output to a payment. That is a structural
property rather than a filter, and structural properties survive questioning in
a way that "we added a guardrail" does not.

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
second the polling loop becomes the bottleneck. A `Queue` protocol with a
Postgres implementation and an SQS implementation (M2) proves the boundary holds.

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

`/healthz` touches nothing. `/readyz` checks Postgres and Redis with a 2-second
timeout and returns 503 if either is down.

Conflating them causes outages: a liveness probe that checks the database will
restart every healthy replica during a failover, turning a recoverable
dependency blip into a full one.

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

---

## Running this in a cloud

| Local | AWS | Code delta |
|---|---|---|
| MinIO | S3 | endpoint URL |
| Postgres container | RDS Postgres | connection string |
| `job` table + `SKIP LOCKED` | SQS + visibility timeout + redrive | queue driver (behind an interface) |
| Redis container | ElastiCache | connection string |
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
  observability/  structured logging; traces and metrics land here in M5
  config/         twelve-factor settings
migrations/       Alembic, reversible, one transaction per upgrade
infra/local/      Keycloak realm and other local-only fixtures
scripts/          seed, smoke test
tests/unit/       no stack required, runs on every commit
tests/integration/ requires `make up`
```

`domain/` depends on nothing. `adapters/` depends on `domain/`. `api/` and
`workers/` depend on both. Dependencies point inwards, which is what makes the
rules engine testable without a database and the queue swappable without
touching the classifier.
