# Jobs are dying

**Alerts:** `JobsDeadLettering` (page) · `DeadLettersUnreplayed` (ticket)

A dead letter is work the system has given up on: a job that exhausted its three
attempts, or one that failed in a way no retry can fix. Each one is an invoice
that entered the system and stopped. Nothing will pick it up again without a
person, which is the whole point — the alternative is an invoice retrying
silently for ever.

The two alerts are different problems. `JobsDeadLettering` means jobs are dying
**now**. `DeadLettersUnreplayed` means jobs died **earlier** and nobody has
looked. Fix the first; work through the second.

## Look at what died

```bash
make dlq
```

Every row carries `failure_kind` and `final_error`. Read the errors before
replaying anything — a batch that all failed the same way is one cause, and
replaying them one at a time is doing the same work three times.

## Classify it

The worker splits failures deliberately, and the split tells you what to do.

**`permanent`** — the handler decided no retry could help. A corrupt PDF, a
document row that no longer exists, an extraction that cannot be read. Retrying
was skipped on purpose: three attempts against the same bytes fail identically
and only delay the invoice reaching a human by the length of the backoff.
*Action:* the invoice needs a person, not a replay. Chase the sender for a
readable copy.

**`transient`** — three attempts were used and the last one still failed.
Storage unreachable, a database blip, an unclassified exception in the handler.
*Action:* if the underlying cause is fixed, replay. If it is not, fix it first
or the replay burns another three attempts.

**`no handler registered for kind ...`** — the worker is running older code than
whatever enqueued the job. *Action:* this is a deployment problem, not a data
problem. Get the worker onto the build that knows the kind, then replay.

## Replay

```bash
make replay id=<uuid>      # one
make replay id=--all       # everything unreplayed
```

A replay is a deliberate human decision and is recorded as one. The dead-letter
row is marked rather than deleted, so the history of what failed and when
survives the fix. The replayed job gets a fresh attempt budget and **starts its
own trace**, linked back to the one that died — see D-021, and expect the new
work under a new trace id rather than inside last week's.

## What not to do

**Do not replay without reading the error.** `--all` against an unfixed cause
turns one incident into the same incident with a bigger dead-letter table.

**Do not delete dead letters.** Succeeded jobs are purged; dead ones never are.
They are the rows somebody still has to look at.

## Where it shows

Grafana → [Threshold](http://localhost:3001/d/threshold-overview) → **Dead
letters**, **Job outcomes** (the `dead_lettered` series).
