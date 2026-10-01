# Every invoice is raising blocking exceptions

**Alert:** `BlockingExceptionSurge` (ticket)

More than three blocking exceptions per invoice on average, sustained for
fifteen minutes. The rule is a **ratio**, not a rate, so this did not fire
because volume went up — it fired because something is wrong with *every*
invoice rather than with some of them.

That distinction is the whole diagnosis. A per-invoice problem is a bad invoice.
A per-batch problem is a bad feed, and a bad feed is usually one upstream change
made an hour ago.

## Find the code

```
topk(5, sum by (code) (rate(threshold_match_exceptions_total{severity="block"}[15m])))
```

Grafana → **Exceptions by owner**, grouped by `code`. One code will dominate.

## Read the code

The taxonomy is frozen and each code means one thing (`domain/taxonomy.py`).
The ones that arrive in bulk:

| Code | What changed upstream |
|---|---|
| `po_not_found` | The purchase order feed is stale or a numbering scheme changed |
| `vendor_unknown` | A vendor import failed, or `legal_name` was bulk-edited |
| `currency_mismatch` | A supplier began billing in a different currency |
| `receipt_missing` | The goods receipt feed is behind |
| `duplicate_suspected` | A batch was submitted twice |
| `subtotal_mismatch` | A layout whose line table is now read only in part (the detail says how many lines were read), or a supplier who began printing freight or a discount outside the lines |
| `bank_details_changed` | **Stop.** See below. |

## If the code is `bank_details_changed`

Treat a surge of these as a security incident until proven otherwise, not as a
data-quality problem. Payment-detail changes arriving in bulk are what invoice
fraud looks like from inside the system, and the control exists for exactly this
case. Do not clear them to reduce the queue. Escalate to whoever owns supplier
onboarding and verify out of band — by a phone number you already held, not one
printed on the invoice.

## Then

Once the code is identified, this stops being an alerting problem and becomes
an upstream one: find the feed or the import that changed, fix it, and the
invoices clear on the next match. They do not need to be re-uploaded — a
re-extraction produces a fresh match, which is why the dedupe key is the
extraction rather than the document.

## What not to do

**Do not resolve exceptions in bulk to clear the board.** An exception is a
control that fired; resolving it without fixing the cause is the same as
disabling the control, except harder to notice afterwards.

## Where it shows

Grafana → [Threshold](http://localhost:3001/d/threshold-overview) →
**Exceptions by owner**, **Match dispositions**.
