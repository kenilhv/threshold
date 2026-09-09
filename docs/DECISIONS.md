# Decision record

One entry per decision that would be expensive to reverse, or that a reader
would otherwise have to reconstruct from the code. Newest first.

---

## D-006 — Store the object before writing the row

**Date:** 2026-09-09 · **Status:** accepted · **Has a known, unpaid cost**

Ingestion hashes first, writes to object storage second, inserts the document
row third. The reverse order would leave a row pointing at bytes that were never
stored — a dangling reference that surfaces much later as an unexplained 404 in
a reviewer's console, by which point the document is simply gone.

This ordering trades that for the opposite failure: when extraction raises and
the transaction rolls back, the object survives with no row referring to it. The
integration suite demonstrates it — a deliberately corrupt PDF returns 422, and
the run ends with six objects against five document rows.

An orphan wastes storage and is reclaimable by diffing bucket keys against
`document.storage_key`. A row promising bytes nobody has is unrecoverable. The
trade is deliberate, but **the reclaiming sweep does not exist yet** and should
land before this runs unattended for any length of time.

---

## D-005 — Idempotency at two layers, answering different questions

**Date:** 2026-09-09 · **Status:** accepted

`Idempotency-Key` answers *"is this the same request?"* — a client retrying
after a timeout gets its original response rather than a second document. The
content hash answers *"are these the same bytes?"* — a supplier resending a PDF
under a new filename resolves to one document with no key involved.

Either alone leaves a hole. A retry with a fresh key creates a duplicate without
the hash; two genuinely different documents sent under one key collide without
the fingerprint.

The header follows Stripe's published semantics, including the case
implementations usually miss: a key reused for *different* content is a 409,
never a silent replay. Handing a caller another invoice's id under a 200 gives
it no way to detect the mistake.

A third layer arrives in M8 as a unique constraint on the payment row. The three
catch a retried HTTP request, a redelivered queue message, and a logic error
respectively. None is redundant with the others.

---

## D-004 — Confidence is per field, and the gate reads the minimum

**Date:** 2026-09-09 · **Status:** accepted

A document-level confidence score cannot answer the only question that matters
operationally: *may this specific value be used to make a decision?* An invoice
can have a perfectly legible total and an illegible purchase-order number, and
those two facts route differently.

So every extracted field carries its own confidence, and the auto-approval gate
reads the **minimum** across decision-relevant fields — never a mean. A mean
lets four confident fields carry one unreadable total past the threshold, which
is precisely the failure this system exists to prevent.

The threshold itself is currently a constant named `PROVISIONAL_CONFIDENCE_GATE`
so that its placeholder status is visible at every call site. M6 replaces it
with an operating point derived from a calibration curve.

---

## D-003 — A deterministic extraction tier that earns its confidence

**Date:** 2026-09-09 · **Status:** accepted

The always-available tier reads the PDF text layer and matches labelled
patterns. It is genuinely worse than a vision model and is labelled worse
everywhere its output is stored.

It exists so CI exercises the real pipeline — ingest, extraction, persistence
and the confidence gate — with no weights, no API key and no network. The
alternative is mocking away the one component the system is built around. It is
also the floor any model tier is measured against, which turns "the model helps"
into a number.

Its confidence is derived from observable signal rather than returned as a
constant: label adjacency, arithmetic corroboration when subtotal plus tax
reconciles to the total, a penalty when several candidates compete for one
field, and lower confidence for genuinely ambiguous date formats. A hard-coded
0.9 would be decoration that M6's calibration work would immediately expose.

---

## D-002 — Immutable, versioned extractions

**Date:** 2026-09-09 · **Status:** accepted

Re-extracting a document inserts version N+1 rather than mutating N, enforced by
a database trigger rather than by convention.

This is what makes the evaluation harness possible: any historical document can
be replayed against a new model or prompt and the two rows diffed. Without it,
"the model changed and the numbers moved" is a mystery rather than a query.

---

## D-001 — Migrations are their own unit of work

**Date:** 2026-09-09 · **Status:** accepted

`migrate` is a separate compose service gated on
`condition: service_completed_successfully`, never an application-startup side
effect. Running migrations at startup means two API replicas race to migrate the
same database, which is a real outage and an easy one to avoid.

Every migration is reversible and `make migrate-roundtrip` proves it by running
upgrade → downgrade → upgrade. A migration nobody dares reverse is a migration
that gets reversed by hand at 3am.
