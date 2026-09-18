# Measurements

Committed output from `make load-test`, one file per run.

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

## Reproducing them

```bash
make clean && make up && make demo-match && make demo-queue
make load-test count=60 concurrency=8
make load-test count=150 concurrency=16
```

The numbers will differ on other hardware, and they are a floor in any case:
one worker, Docker Desktop, sharing a CPU with Postgres, MinIO, Keycloak,
Grafana, Tempo and Prometheus, nothing tuned. What should hold anywhere is the
*shape* — throughput rising with offered load until the worker saturates, and
backlog arriving as settle latency rather than as errors.
