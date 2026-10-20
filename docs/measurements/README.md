# Measurements

Committed output from `make load-test` and from `make aws-verify`, one file per
run.

The README quotes these figures, and a quoted figure with no file behind it
cannot be checked. Every other measured claim in this repository is defended by
a mechanism — the extraction baseline is diffed by `make eval-check` on every
commit, `smoke.sh` counts its own checks rather than asserting a total — and
until M10 the latency table was the exception.

## Reading them

`conditions` comes first in each file on purpose. The field that moves these
numbers most is not the CPU, it is `documents_before`: an early run reported a
settle p50 of 5.6s against a database holding 4,317 documents of test residue,
and nothing in the output said so. Two runs are only comparable if that number
is close.

`load-150x16.json` starts at 219 documents because it inherits the 60 invoices
the other run submitted. That is recorded rather than reset, because resetting
between runs would hide the effect the field exists to expose.

`aws-staging-<date>.json` is a different instrument and says so. `conditions`
records the image, the task counts and sizes, the RDS class, the Postgres
version and the schema revision, read back from the deployment rather than
assumed; the measuring machine's address is not recorded. `submit` is what the
laptop saw over the public internet: accept latency and status counts.
`pipeline` is what `scripts/aws_verify.py` read from inside the VPC, scoped to
exactly the documents that run submitted: counts at each stage, settle latency
from the audit trail with the same query the laptop runs use, payments by
state, dead letters, the trial balance, and the database roles' privilege
flags. The accept figures are not comparable with the laptop's, which never
left the machine; the settle figures are, and are slower, for the reasons the
file's `conditions` show.

## Reproducing them

```bash
make clean && make up && make demo-match && make demo-queue
make load-test count=60 concurrency=8
make load-test count=150 concurrency=16
```

```bash
make aws-up && make aws-verify && make aws-down   # needs AWS credentials; about $0.30 an hour while up
```

The numbers will differ on other hardware, and they are a floor in any case:
one worker, Docker Desktop, sharing a CPU with Postgres, MinIO, Keycloak,
Grafana, Tempo and Prometheus, nothing tuned. What should hold anywhere is the
*shape* — throughput rising with offered load until the worker saturates, and
backlog arriving as settle latency rather than as errors.
