# Threshold

**An accounts-payable control plane.** It takes an invoice from arrival to
payment without a human touching it — unless it cannot prove it should.

The name is the argument. The system's entire job is to hold a defensible line
between what a machine may execute alone and what a human must decide, and to
show where that line is and why it sits there.

> **Status: M0.** The stack runs, the schema migrates, the seed loads. Document
> ingestion is M1 and the milestones below are honest about what does not exist
> yet. Every claim in this README is checkable against the repository today.

---

## Run it

```bash
make up
```

That is the whole thing. It builds the images, starts Postgres, Redis, MinIO and
Keycloak, waits for each to pass its own healthcheck, applies the migrations,
loads reference data, then starts the API and the worker.

| | |
|---|---|
| API docs | <http://localhost:8000/docs> |
| Readiness | <http://localhost:8000/readyz> |
| MinIO console | <http://localhost:9001> — `minioadmin` / `minioadmin` |
| Keycloak | <http://localhost:8080> — `admin` / `admin` |

Then prove it actually works rather than merely started:

```bash
make smoke
```

Other useful targets: `make logs`, `make ps`, `make down`, `make clean`
(destroys volumes), `make migrate-roundtrip`, `make verify`.

Requires Docker with Compose v2. No cloud account, no API keys, no paid
services.

---

## The design in one page

An invoice arrives, is extracted by a model that reports per-field confidence,
is matched against its purchase order and goods receipt by **deterministic
rules**, and is either auto-approved under an explicit delegation limit or
turned into **one typed exception with a named owner** who reviews it.

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
- [ ] M1 — ingestion and schema-constrained extraction with per-field confidence
- [ ] M2 — durable job execution: Postgres queue, leases, backoff, dead-letter, replay
- [ ] M3 — three-way match, the frozen exception taxonomy, owner routing
- [ ] M4 — the operator console
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
| Redis container | ElastiCache |
| Compose services | ECS Fargate tasks behind an ALB |
| Env files | Secrets Manager |
| Keycloak | Cognito, or an existing IdP |

The queue driver and the secret source are the only two places the code paths
genuinely differ, and both sit behind an interface. Terraform describing the AWS
shape is validated in CI from M10 and deliberately never applied — spending
money to host a demo proves less than the interface that makes it a
configuration change.
