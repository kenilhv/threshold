# The AWS shape

Terraform describing where this system would run, **validated in CI and
deliberately never applied**.

That is a choice rather than a shortcut. Applying it would cost money to host a
demo nobody visits, and would prove less than the thing actually worth proving:
that moving from the compose stack to AWS is a change of configuration and not a
change of code. What is checked here is `terraform init -backend=false`,
`validate` and `fmt -check` — that the configuration is well-formed, internally
consistent and says what it means. What is not checked is that AWS would accept
it, and no claim is made that it would.

`make tf-validate` runs the same three commands the CI job runs, through the
pinned `hashicorp/terraform` image, so no local install is required.

## What it describes

| Local (compose) | Here |
|---|---|
| MinIO | S3, versioned, encrypted, public access blocked |
| Postgres container | RDS Postgres 16, private subnets, encrypted, 7-day PITR |
| `job` table + `SKIP LOCKED` | **still the `job` table** — see below |
| `api` / `worker` services | ECS Fargate services, the API behind an ALB |
| `migrate` / `seed` one-shots | ECS task definitions, run on deploy |
| env files | Secrets Manager, one secret per database role |
| Keycloak container | not described: an existing IdP is an input, not infrastructure |

## The queue stays in Postgres, and the README used to say otherwise

Until M10 the README's cloud table said the `job` table becomes "SQS with a
visibility timeout and redrive policy", and that the queue driver "sits behind
an interface" so the swap is configuration.

The interface is real: `adapters/queue.py` defines `Queue` and `ReapableQueue`
as Protocols, and `api/dependencies.py` resolves one implementation. What was
not true is that swapping it is free, and writing the Terraform is what made
that concrete:

- Enqueue happens **inside the transaction that writes the row the job is
  about** — `services/ingest.py` stores the document and enqueues extraction
  atomically, and `workers/handlers.py` enqueues the next stage in the same
  transaction as the result of the current one. With SQS the message is sent
  outside the database transaction, so the two can disagree: a committed
  document with no message, or a message for a row that rolled back. Recovering
  that is the transactional-outbox pattern, which is *more* moving parts than
  the table it replaces.
- `uq_job_dedupe_key` refuses a duplicate job at the index. SQS FIFO
  deduplicates within a five-minute window, which is a different guarantee.
- The dead-letter table is queryable by `make dlq` and replayable by id. A
  redrive policy is neither.

So the module describes an RDS instance sized for the queue as well as the
ledger, and the migration to SQS is listed as a decision somebody would make on
purpose, with the outbox it requires. That is a more useful thing for this
repository to say than a table cell claiming a swap that would lose two
guarantees.

## The role split survives the move

Migration 0012 gives the API and the worker separate database roles. Here that
becomes two Secrets Manager secrets and two task roles: the API task cannot read
the worker's secret and vice versa, so the boundary is enforced by IAM as well
as by Postgres. That is the one place where deploying this properly makes a
control *stronger* rather than merely preserving it.

## Files

```
main.tf        providers, tags, and the inputs every other file reads
network.tf     VPC, subnets, endpoints — no NAT gateway, see the comment
data.tf        RDS and S3
compute.tf     ECS cluster, task definitions, services, ALB
secrets.tf     Secrets Manager, and the two task roles that read one each
variables.tf   every input, with a comment on why it is an input
outputs.tf     what a deploy would hand back
```
