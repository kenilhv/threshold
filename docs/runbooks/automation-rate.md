# The system stopped clearing invoices on its own

**Alert:** `AutoApprovalRateCollapsed` (ticket)

Fewer than one invoice in five cleared without a person over the last hour,
against a baseline near two in five. Nothing is down. The system is working
exactly as designed and deciding, correctly, that it cannot clear these
invoices — which means the interesting question is what changed about the
invoices, the policy, or the reference data.

This is the metric the product exists to move, so treat a collapse as a product
incident rather than an infrastructure one.

> **On the threshold.** The 20% floor is calibrated against the local fixture
> mix, not measured from production. It is deliberately half the observed
> baseline so ordinary variation in invoice mix cannot reach it. In a real
> deployment this number comes from the real baseline, and moving it is moving
> the definition of "working" — which is a decision, not a tuning exercise.

## Find out which control started firing

The automation rate falling always means some exception started being raised.
Group the exception counter by code:

```
sum by (code) (rate(threshold_match_exceptions_total[1h]))
```

Grafana → **Exceptions by owner**, then group by `code`. The shape of the answer
tells you the cause.

## The four usual causes

**A tolerance policy changed.** Check the `policy_version` label on
`threshold_match_decisions_total`: if the rate fell at the moment a new version
appeared, the policy is the cause. Compare against `docs/DECISIONS.md` — D-014
records that tolerances are versioned precisely so this question has an answer.

**The vendor master stopped resolving.** A spike in `vendor_unknown` means
invoices no longer match a supplier row. Usually a failed or partial vendor
import. Resolution is by tax identifier first and exact name second, so a
bulk-renamed `legal_name` breaks the second path without touching the first.

**Purchase orders stopped arriving.** A spike in `po_not_found` or
`no_po_reference` means the PO feed is stale. The matcher is working; it has
nothing to match against.

**Goods receipts stopped arriving.** A spike in `receipt_missing` means the
receiving feed is behind. The invoices are fine and will clear once receipts
land.

## What not to do

**Do not widen tolerances to make the alert stop.** That converts a visible
problem into an invisible one — invoices clearing that should not have. The
tolerance policy is versioned and audited for exactly this reason.

**Do not bulk-approve to clear the queue.** Every decision is appended to
`approval_decision` with the actor on it, permanently.

## Where it shows

Grafana → [Threshold](http://localhost:3001/d/threshold-overview) →
**Automation rate**, **Match dispositions**, **Exceptions by owner**.
