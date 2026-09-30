# A payment is stuck

**Alerts:** `PaymentStuckInFlight` (a payment has been awaiting the rail for
over fifteen minutes) and `PaymentObserverStopped` (nothing is publishing
payment counts at all, so money may be moving unwatched).

Fifteen minutes is measured against the executor rather than picked round: the
job's backoff is capped at ten, so anything under that is a payment that has
simply not had its second attempt yet, and alerting there would page on the
design working.

## What "stuck" means here

One state, and it is not an error: **`submitting`**.

A payment in `submitting` has had its ledger posting written and its instruction
sent, and the rail has not answered. It is not a failure — a failure is
`failed`, which means the rail looked at it and said no. `submitting` means *we
do not know*, and that is the only honest thing the row can say.

Everything else is either moving or finished:

| state | meaning | stuck? |
|---|---|---|
| `pending` | authorised, nothing posted, nothing sent | only if the job queue is down |
| `submitting` | posted and sent, no answer | **this runbook** |
| `submitted` | the rail accepted it | only if the job queue is down |
| `settled` | confirmed, terminal | no |
| `failed` | refused, reversed, terminal | no |

## The first thing to know

**Do not create a second payment.** Four separate mechanisms exist to stop you
and you should not be the thing that defeats all four:

* `uq_payment_authorisation` — one payment row per approval decision.
* `idempotency_key` — derived from that same approval decision, so every retry
  sends the rail the identical key and a rail that already took the money
  returns its original answer rather than taking it again.
* `uq_payment_live_per_match` — at most one live payment per body of evidence.
* `uq_payment_live_per_document` — at most one live payment per document,
  whatever authorised it. A new approval for an invoice already in flight is
  refused and dead-lettered rather than paid (D-037), so re-approving is not a
  way round a stuck payment.

Retrying is safe *because* nothing about the retry is new. Anything that
generates a fresh key, a fresh payment row or a fresh approval defeats that.

## Check, in order

**1. Is it actually stuck, or just recent?**

```sql
select id, state, rail_attempts, created_at, submitted_at, now() - submitted_at as age
from payment where state = 'submitting' order by submitted_at;
```

`rail_attempts` is the number that matters. One attempt and thirty seconds is a
payment in flight. Twenty attempts is an outage.

**2. Is the worker running and claiming jobs?**

```bash
docker compose ps worker
```

```sql
select state, count(*) from job where kind = 'execute_payment' group by 1;
```

A payment sits in `submitting` forever if nothing is advancing it. That is a
queue problem, not a payment problem — see the queue-backlog runbook.

**3. Did the job die?**

```sql
select left(last_error, 200), count(*) from job
where kind = 'execute_payment' and state = 'dead' group by 1 order by 2 desc;
```

Two errors have been seen in practice and mean opposite things:

* `no handler registered for kind 'execute_payment'` — a deploy-ordering
  mistake. The enqueue shipped before the handler. Deploy the worker, then
  replay the dead letters; the payments are intact and unstarted.
* `no approval decision <uuid>` — the authorisation was deleted under the job.
  Correct behaviour: the job is permanently dead and must not be replayed.

**4. Do the books still balance?**

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8000/v1/ledger/trial-balance
```

`balanced: false` changes the priority of everything above. Stop, and see
"the books do not balance" below.

## What to do

**The rail is down.** Nothing. The payment stays in `submitting`, the job keeps
retrying with the same key, and it resolves when the rail does. Watch
`rail_attempts`; if it climbs past what the rail's own incident page justifies,
escalate to whoever owns that relationship.

**The job is dead and the handler now exists.** Replay it. The payment resumes
from whatever state it was in, which is the whole point of the state being
durable:

```sql
select id, kind, left(last_error, 80) from dead_letter
where kind = 'execute_payment' and replayed_at is null;
```

Replay through `PostgresJobQueue.replay_dead_letter`, not by inserting a job by
hand — the replay records that it happened.

**The rail says it never received it.** Then the money did not move, and the
payment should end in `failed` with that as its reason. There is no operator
path for this yet and writing one by hand means an `UPDATE` on `payment` and a
reversing ledger transaction, in that order, in one transaction. Do not delete
the submission posting; the ledger is append-only and
`ck_ledger_entry_balanced` will refuse the deletion anyway.

**The rail says it was paid.** Then the answer was lost, not the payment.
Advancing the job again re-asks with the same key and the rail returns its
original acceptance, which settles it normally. This is the case the design is
for; let it run.

## The books do not balance

`trial-balance` returning `balanced: false` means something reached
`ledger_entry` without going through either `domain/ledger.py` or the deferred
trigger — which, given the trigger, is a short list: a restore from a bad
backup, a migration that moved rows, or manual SQL during an earlier incident.

```sql
select transaction_id, sum(amount_minor) from ledger_entry
group by 1 having sum(amount_minor) <> 0;
```

Do not "fix" the imbalance by editing entries. Find out what wrote them. A
ledger you have corrected by hand is a ledger whose totals are your opinion, and
the reason this system uses double-entry at all is to not have one of those.

## What this runbook cannot tell you

Whether the supplier was actually paid. This system knows what the rail told it
and when, and nothing more. A payment in `submitting` for an hour is a phone
call to the bank, not a query.
