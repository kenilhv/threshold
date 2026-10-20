# The AWS shape

Terraform describing where this system runs when it is not on a laptop:
**validated in CI on every push, applied on demand, destroyed afterwards.**

```
make aws-plan     # what aws-up would create; creates nothing
make aws-up       # build, push, apply, migrate, seed, wait for the API
make aws-verify   # submit invoices, measure the pipeline from inside the VPC
make aws-down     # destroy it all
```

Each needs AWS credentials in the environment or `~/.aws`, Docker with its
daemon running, and this project installed with `pip install -e .[dev,fixtures]`
(the verification renders invoices with reportlab). A `terraform` binary is
used if there is one; otherwise the pinned `hashicorp/terraform` image the CI
job uses. Nothing takes a credential as an argument, and `aws-up` and
`aws-down` ask before they act.

**Who can reach it.** `POST /v1/documents` takes no credential by design, so an
internet-facing staging stack would be a free upload service for anyone who
found the hostname: every distinct body becomes an object in a versioned
bucket, a row, and a job for the worker, with no WAF and no rate limit in
front. By default the load balancer therefore admits only the machine running
the script (its public `/32`, looked up at apply time); `--ingress open` is the
deliberate exception, and `--ingress a.b.c.d/32,...` names others.

## What a deploy proves, and what it costs

The claim worth proving is that moving from the compose stack to AWS is a change
of configuration and not a change of code. `aws-up` runs the same image, the
same migrations and the same seed against RDS and S3, with the API behind an ALB
and the worker claiming from the same Postgres queue. `aws-verify` then submits
invoices over the public internet, waits for the pipeline inside the VPC, and
writes what it saw to `docs/measurements/`.

It costs money while it exists. In `eu-west-1` with the defaults: an ALB
(~$0.025/h), a `db.t4g.medium` (~$0.07/h), three Fargate tasks (~$0.10/h
together), and four interface endpoints across two AZs (~$0.08/h), roughly
**$0.30 an hour, about $7 a day**, plus pennies of storage and logs. Those are
list-price estimates, not a bill; `aws-down` is what stops the meter, and the
Cost Explorer lags a day behind it.

## What is deliberately left out

- **No identity provider.** Keycloak is a compose container, not an AWS
  resource, and standing one up is not this module's decision. With no issuer
  configured the API refuses every authenticated route with `503` rather than
  waving anyone through ([api/auth.py](../../src/threshold/api/auth.py)), so a
  deploy serves ingest and health, and the console is not reachable. The
  pipeline behind ingest (extract, match, decide, pay, post) is the whole of the
  worker, and that is what `aws-verify` measures. Setting `oidc_issuer` also
  needs a route to its JWKS, which the VPC does not have (next point).
- **No NAT gateway.** Tasks run in private subnets with no route to the
  internet and reach ECR, Secrets Manager, CloudWatch Logs and S3 through VPC
  endpoints. A compromised worker has nowhere to send anything. See the comment
  at the top of `network.tf`, including the egress rule for the S3 gateway's
  prefix list, without which a task can neither reach the bucket nor pull its
  own image layers.
- **No domain.** Without `api_domain_name` the API is served over plain HTTP on
  the load balancer's hostname, to the addresses above. With one, an ACM
  certificate is requested with DNS validation, and because a listener cannot
  attach a pending certificate the first apply fails at the HTTPS listener;
  the `certificate_validation_records` output names the CNAME to create, and a
  second apply after ACM issues it succeeds. Two applies, stated rather than
  hidden; automating it needs a Route 53 zone this module does not assume.
- **No telemetry backend.** Spans and metrics are produced and not exported
  unless `otel_traces_endpoint` / `otel_metrics_endpoint` point somewhere.

## What is different from the first draft of this module

The first draft was validated and never applied, and applying it would have
failed. Writing `aws-up` and having the result reviewed is what found most of
the gaps; applying it found the last two.

**Applied once: 2026-10-20, `us-west-2`, about an hour from `aws-up` to
`aws-down`.** Three applies reached a healthy load balancer. The first stopped
at a security-group rule whose description contained an apostrophe, which EC2
refuses; the second at the migrate task, which could not pull its image because
the S3 gateway policy confining the tasks to this account also refused the
presigned URLs ECR serves image layers through. Both are fixed below and in
the table. The measurement that run produced is
[`docs/measurements/aws-staging-20261020-1827.json`](../../docs/measurements/aws-staging-20261020-1827.json),
and [D-041](../../docs/DECISIONS.md) is the record. The hour cost roughly
thirty cents at list price; the bill arrives a day later.

| Was | Is |
|---|---|
| `THRESHOLD_S3_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` required by Settings and set nowhere, so the process could not start | optional; the task role is the credential ([config/settings.py](../../src/threshold/config/settings.py)) |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, which the code never read | the two `THRESHOLD_OTEL_*_ENDPOINT` settings the code reads, both optional |
| issuer set, JWKS URL not, so every token would have been refused | both or neither |
| migrate task read a `url` key from the RDS master secret, which has `username` and `password` only | every task receives the database as parts and Settings composes the URL, quoted |
| role passwords "written out of band after the first migrate" | generated here and handed to the migrate task, so `aws-up` is one command (trade-off in `secrets.tf`) |
| an ECR URL as an input, with nothing building or pushing | the repository is a resource, `aws-up` builds and pushes the commit |
| a bucket named `threshold-<env>-documents`, which is global and taken by whoever applies first | suffixed with the account id |
| task egress to 443 within the VPC CIDR only | plus the S3 gateway prefix list |
| VPC endpoints with the default allow-any-principal policy, under a comment claiming "nowhere to send anything" | every endpoint refuses principals outside this account, and the S3 gateway additionally admits reads of the regional bucket ECR serves image layers from, because those requests are signed by ECR and the first policy refused them |
| an apostrophe in a security-group rule description | none; EC2 refuses them |
| the load balancer open to the world | the operator's address unless opened on purpose |
| the Alembic environment fed the URL through a `ConfigParser`, which reads a percent-encoded password as interpolation syntax and raises with the URL in the traceback | the URL goes straight to SQLAlchemy |
| the RDS Postgres log group created by RDS, outside Terraform, with no retention | declared, 30 days, destroyed with the rest |
| HTTPS listener on a certificate nobody could validate | HTTP on the ALB hostname unless a domain is given |

## The queue stays in Postgres

An earlier README said the `job` table becomes "SQS with a visibility timeout
and redrive policy" and that the queue driver "sits behind an interface" so the
swap is configuration. The interface is real (`adapters/queue.py`); the swap is
not free, and writing this module is what made that concrete:

- Enqueue happens **inside the transaction that writes the row the job is
  about**. With SQS the message is sent outside the database transaction, so
  the two can disagree: a committed document with no message, or a message for
  a row that rolled back. Recovering that is the transactional-outbox pattern,
  which is more moving parts than the table it replaces.
- `uq_job_dedupe_key` refuses a duplicate job at the index. SQS FIFO
  deduplicates within a five-minute window, which is a different guarantee.
- The dead-letter table is queryable and replayable by id. A redrive policy is
  neither.

So the module describes an RDS instance sized for the queue as well as the
ledger, and the migration to SQS is listed as a decision somebody would make on
purpose, with the outbox it requires.

## The role split survives the move

Migration 0012 gives the API and the worker separate database roles. Here that
becomes two Secrets Manager secrets and two task roles: the API task cannot read
the worker's secret and vice versa, so the boundary is enforced by IAM as well
as by Postgres. That is the one place where deploying this properly makes a
control *stronger* rather than merely preserving it.

## Files

```
main.tf        providers, tags, and the environment every task shares
network.tf     VPC, subnets, endpoints -- no NAT gateway, see the comment
data.tf        ECR, S3 and RDS
compute.tf     ECS cluster, task definitions, services, ALB
secrets.tf     role passwords, and the task roles that may read exactly one
variables.tf   every input, with a comment on why it is an input
outputs.tf     what a deploy hands back; ARNs, never values
```

The deploy and verification scripts are `scripts/deploy_aws.py` and
`scripts/aws_verify.py`.
