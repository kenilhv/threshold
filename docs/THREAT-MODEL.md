# Threat model

This is the M9 deliverable that is prose rather than code, and it is the one
that decides whether the rest of the milestone was aimed at the right thing.

It is written to be argued with. Every control below points at the migration,
module or test that implements it, so a claim can be checked rather than
believed. Where an attack works, it says so and says what bounds the damage.
Several of the things in the "works" column were found by running them against
this schema, not by imagining them.

**Scope.** The system as it exists in this repository: ingest, extraction,
three-way match, human approval, payment execution against a sandbox rail, a
double-entry ledger, a triage agent, and the console in front of them. Not in
scope: the production deployment, which does not exist (M10), and the identity
provider, which is Keycloak and is somebody else's threat model.

---

## 1. What this system is trusted with

In order of how badly it goes wrong.

1. **The authority to move money.** An approval decision releases a payment.
   The system decides autonomously which invoices need no person at all.
2. **Where the money goes.** A vendor's bank details, and the judgment that a
   change to them is or is not legitimate.
3. **The record of who decided what.** The thing a dispute, an audit or an
   investigation is resolved against.
4. **Commercially sensitive data.** Who supplies this company, at what prices,
   on what terms — inferable from invoices alone.
5. **Availability.** An accounts-payable system that stops does not lose money
   directly; it loses discounts, relationships and eventually goods.

The rest of this document is about (1) and (2). They are the ones where an
attacker profits.

---

## 2. Trust boundaries

```
                                             ┌─────────────┐
  supplier ──(hostile bytes)────────────────▶│     API     │──▶ object store
                                             │             │        ▲
  staff ──(session cookie)──▶ console ──────▶│             │        │ reads
                              (holds the     └──────┬──────┘        │ the bytes
                               bearer token)        │ threshold_api │
                                                    ▼               │
                                                 Postgres ◀─────────┼── threshold_worker
                                                    ▲               │
                                             ┌──────┴──────┐        │
                                             │   worker    │────────┘
                                             │             │──▶ payment rail (sandbox)
                                             │             │──▶ model tier (none today)
                                             └─────────────┘
```

The job queue is a table in Postgres, which is why it is not drawn as a separate
box: that is the point of D-003, and an arrow would imply a broker that does not
exist.

Six boundaries, each with a different assumption behind it.

**Supplier → API.** Everything arriving here is hostile. An invoice is a file a
stranger sent, and the whole pipeline behind it is written on that assumption.
`POST /v1/documents` is deliberately unauthenticated — suppliers are not users
of this system — and `tests/unit/test_route_authentication.py` makes that a
written decision rather than an omission. That test is how two open `GET` routes
were found: `GET /v1/documents/{id}` had been answering anybody since M1 and
`GET /v1/documents/{id}/match` since M3, because nobody had ever enumerated the
surface. Both returned the vendor, the amount and every exception's free text.

**Staff → console → API.** Staff never hold a bearer token. The console is a
back-end for front-end: it performs authorization code with PKCE against
Keycloak, keeps the access token in an httpOnly cookie on its own origin, and
calls the API from the server (`console/lib/session.ts`). The browser holds a
session cookie and nothing else.

Authentication is delegated to Keycloak; roles come from `realm_access.roles`
and anything unrecognised is dropped (`src/threshold/api/auth.py`). The system
trusts the token's claims about who somebody is. It does not trust the client
about what they may do: that is `authorise()`, and since M9 the database re-runs
it.

**A compromised console is its own blast radius**, and a different one from a
compromised API: it holds every signed-in reviewer's access token, so it can act
as any of them for as long as their tokens live. It holds no database credential
at all. Section F.

**Process → Postgres.** The API and the worker hold separate roles with
separate grants (D-035). Neither owns a table, neither is a superuser, and the
rules about what each may write are enforced by Postgres rather than by the
code that would be compromised first.

**System → model tier.** There is no model tier today; the triager is
rule-based. The boundary is built anyway, because the point of building it
before there is a model is that the containment is structural rather than
retrofitted. The agent has no write tools, its evidence is fetched up front
through a registry (D-030), and what it says is validated against what was
recorded before anything acts on it (D-031).

**System → payment rail.** The sandbox rail is in-process, and the worker is
what calls it. A real rail is a network call that may take money and not answer,
which is why a payment is committed as `submitting` *before* the call and
retried with a key derived from the approval decision
(`services/payments.py`, `domain/payments.py::idempotency_key`, and D-005 on
why idempotency lives at two layers).

---

## 3. Adversaries

Ordered by how likely they are to actually turn up.

### A. A supplier who wants to be paid more, or paid elsewhere

The ordinary adversary, and the one the product exists for. No system access.
Sends documents.

| Attack | Control | Evidence |
|---|---|---|
| Invoice above the agreed price | Three-way match against the PO, with a tolerance | `test_matching.py` |
| Invoice for goods never received | Match against goods receipts | `test_matching.py` |
| Same invoice twice, identical bytes | A unique index on the content hash | `document_content_sha256_key`, `services/ingest.py` |
| Same invoice twice, re-rendered | Vendor name *as read* plus invoice number | `duplicate_suspected`, `services/matching.py` |
| New bank details on the invoice | Compared against the vendor's history; `bank_details_changed` blocks and is treasury's | `services/matching.py`, `test_matching.py` |
| Prose in the PDF aimed at the triage agent | The agent cannot write, may cite only codes that were recorded, and may name only an owner that one of *those cited codes* routes to | `test_agent_red_team.py` (13 attacks), D-031 |
| Prose in the *filename* aimed at the agent | The agent is no longer shown filenames at all | D-036, `test_projection.py` |

**What still works.** The duplicate check compares the vendor name *as read
from the document*, not a resolved vendor id (`services/matching.py`), so a
supplier who retypes their own name and re-renders the PDF defeats both layers:
different bytes, different vendor string. What actually stops them being paid
twice is at the other end — `uq_payment_authorisation`, one payment per approval
decision — which means the duplicate reaches a person rather than the bank.

A supplier can put text in front of the agent through an
exception's `detail`, because `detail` contains the invoice's own line
descriptions and that is the evidence a triager reasons about. The red-team
suite treats it as hostile and asserts what bounds it: the agent can influence
*who looks first*, and cannot approve, cannot pay, and cannot name an owner that
none of the codes it cited already routes to.

That last clause is exact on purpose. "An owner the *recorded* exceptions do not
implicate" is the weaker rule, and D-031 records it as the one that failed: on an
invoice carrying both a redirect and a price query the buyer is genuinely
implicated, so a packet citing only the redirect could still name the buyer and
route a treasury problem to the wrong desk. The implemented rule reads the cited
codes.

A supplier who is already on file can also be overpaid if they can get an
invoice auto-approved. Auto-approval requires a purchase order that resolves to
an open order for the same vendor and currency; it does **not** require that a
receipt comparison ran, only that nothing the comparison ran raised anything.

Writing that sentence honestly is what found the worst defect in this document's
history. Until M10, an invoice whose extraction carried *no line items* skipped
price, quantity and receipt matching entirely, raised zero exceptions, and
auto-approved. An invoice for 99,999.99 against a 220.80 order was paid without
a person seeing it. The matcher wrote a note into `skipped_checks` saying
nothing had been compared, and nothing read `skipped_checks`.

A skipped check now raises `checks_incomplete` when it would otherwise have left
the invoice auto-approved, so the default is a person. See
`test_an_invoice_with_no_line_items_is_not_auto_approved`.

### B. A careless or malicious insider

Has a token and a role.

| Attack | Control | Evidence |
|---|---|---|
| Approve an invoice whose exceptions are somebody else's | `authorise()`: the deciders must own *every* open exception | `test_approvals.py`, and now `may_decide()` in Postgres |
| Approve as an auditor | Read-only roles decide nothing | `authorise()`, `test_two_roles.py` |
| Decide twice, or race another reviewer | `uq_approval_decision_terminal`; the loser is told what actually happened | `test_approval_race.py` |
| Edit the audit trail | `UPDATE`/`DELETE` refused by privilege, then by trigger | `test_least_privilege.py`, smoke |
| Change where a pending payment goes | Payee and amount immutable after creation, for the owner too | `test_payment_integrity.py` |
| Reopen a settled payment and be paid twice | Terminal states frozen in the database | `test_payment_integrity.py` |

**What still works.** Everything an insider is *authorised* to do. A buyer who
owns the only exception on an invoice can approve it, and that is the design:
the system routes judgment to whoever holds it, and does not attempt to
second-guess the judgment. Two-person approval for large amounts does not exist
and would be the obvious next control.

Nothing here defends against a person with legitimate access approving a
fraudulent invoice from a vendor already on file. Segregation of duties is the
control for that, and this system implements the routing half of it
(`domain/taxonomy.py`) without the second-signature half.

### C. A compromised worker process

The most likely process to lose, because it parses PDFs sent by strangers. Runs
arbitrary code as `threshold_worker`.

| Attack | Result |
|---|---|
| Disable the append-only trigger, or the ledger's balance trigger | Refused: not an owner (`42501`) |
| Become a superuser, `SET ROLE`, create objects, read `pg_authid`, `COPY … PROGRAM` | Refused |
| Record that a person approved an invoice | **Refused by policy** — the worker may only write `auto_approved` |
| Auto-approve an invoice the match blocked | **Refused by policy** — the policy reads the match result |
| Rewrite a vendor's bank details | Refused: reference data is `SELECT` only |
| Change a payment's amount or payee | Refused: column-level grants, plus a trigger |
| Post a balanced but fictional ledger transaction | Refused: must cite a payment or match result, and match its amount |
| Post an opening balance | Refused by policy |

**What still works, and this is the important paragraph.** The worker decides
what auto-approves — that *is* its job — so a compromised worker can fabricate a
match result that says `auto_approve`, write the `auto_approved` decision the
policy permits, and have the invoice paid. Every control above passes it,
because each row agrees with the rows it cites and the database has no
independent knowledge of what the invoice said.

What bounds it: the payee must be the *current* bank account of the vendor on
the purchase order the match result cites, and `vendor_bank_account` is
`SELECT`-only to both runtime roles. So a compromised worker can overpay a
vendor already on file, into that vendor's own account. It cannot pay a
stranger, and it cannot redirect an existing vendor's payments. **The amount is
not bounded.**

Making money leave requires the attacker to also control a vendor who is already
on file — which is a much smaller set of attackers than "anyone who can exploit
a PDF parser", and is the point of the bound.

### D. A compromised API process

| Attack | Result |
|---|---|
| Write a match result, extraction, payment or ledger entry | Refused: it holds no `INSERT` on any of them |
| Update or delete anything, anywhere | Refused: it holds no `UPDATE` or `DELETE` at all |
| Record an `auto_approved` decision | Refused by policy |
| Record a decision without saying who is deciding | Refused: `may_decide()` fails closed |

**What still works.** The API is the process that knows who a person is, so a
compromised API can claim a person approved something. `may_decide()` is
evaluated against a setting that process controls, so it does not stop this —
D-035 says so rather than implying otherwise, because a control that sounds like
a defence and is not is worse than no control.

It cannot invent the invoice that person appears to have approved, which means
the forged approval has to attach to a real match result — and a real match
result on a fraudulent invoice requires (C).

**The next control, not built:** a `SECURITY DEFINER` function owned by the
schema owner, which would derive the deciding roles from a table given only an
`actor_id` instead of reading a setting the caller supplies. That narrows the
forgery — a compromised API could no longer grant itself arbitrary roles — and
it does not close it, because the `actor_id` still arrives from the API. The
only identity Postgres verifies over a pooled connection is `current_user`, and
that names the process rather than the person.

Closing it properly means the database learning who the person is, which means
the session, which is a different design. Worth saying out loud: the control
after this one is not a bigger trigger.

### E. Anyone who obtains the database credentials

Both runtime roles hold what section C and D describe. The owner credential
lives in two short-lived containers (`migrate`, `seed`) and in developer tooling.

An attacker with the **owner** credential has everything: it may disable every
trigger and rewrite every table. There is no control for this in the schema, and
there cannot be — it is the same problem as root. The control is that no
long-running process holds it, which `scripts/role_probe.py` asserts from inside
the running containers on every `make smoke`.

An attacker with **`pg_dump` access** has the commercial data. Nothing in this
repository encrypts at rest; that is a deployment property and belongs to M10.

### F. A compromised console

The console is the third long-running process, and it was missing from the first
draft of this document — found by checking the trust-boundary diagram against
`console/lib/session.ts` rather than against memory.

It holds **every signed-in reviewer's access token**, in httpOnly cookies on its
own origin, and calls the API from the server. So a compromised console can act
as any currently signed-in person, for as long as their token is valid. Every
control in section B applies to it — it cannot approve an invoice whose
exceptions the borrowed identity does not own, because `authorise()` and
`may_decide()` both run against that identity's roles — but within those bounds
it is indistinguishable from the person.

What it does *not* hold is a database credential. It reaches Postgres only
through the API, so everything in sections C and D bounds it too: it cannot
write a match result, a payment or a ledger entry, because the API it must go
through cannot.

Two things make this smaller than it sounds, and neither is a control anybody
designed for it: tokens are short-lived, and the console is the only process
here with no database access at all. Worth stating anyway, because "the console
is just a front end" is exactly the assumption that would leave it out of a
review.

`scripts/check_console.py` asserts that no restricted value and no access token
ever reaches the browser, which is a different property — it bounds what a
compromised *browser* sees, not what a compromised console does.

---

## 4. What the audit trail is and is not

It is proof against **erasure by the runtime roles**. They hold `SELECT` and
`INSERT` and nothing else, so an `UPDATE` is refused before any trigger is
reached, and the trigger behind that lock stops an ordinary `UPDATE` even when
the owner issues it — the smoke test fires it as the owner every run.

It is not proof against the owner, who can disable the trigger. That is the same
problem as root, and section E is where it belongs. No test covers it because
none can.

It is **not** proof of **authenticity**. Both runtime roles may `INSERT`, and
nothing ties an inserted row's `actor_id` to an authenticated session. A
compromised process can write audit rows naming a real person, and those rows
are as permanent as the honest ones — append-only cuts both ways.

Closing that means writing audit rows through a `SECURITY DEFINER` function that
stamps the actor from verified session context, or a hash chain that makes
independently-inserted rows detectable. Neither is built.

Since migration 0013, two events written in the same transaction can at least be
ordered against each other, which they could not before.

---

## 5. Known weaknesses, unfixed

Written down because a threat model that lists only what is defended is
marketing.

| Weakness | Consequence | Where |
|---|---|---|
| No `SECURITY DEFINER` chokepoint for decisions | A compromised API can forge a human approval | §D |
| Audit rows are not authenticated | A compromised process can write convincing lies | §4 |
| No second signature on large amounts | One authorised person can release any amount | §B |
| `heartbeat()` is never called by the worker loop | A job that outlives the two-minute lease is reaped and run again while the first attempt is still working. Exercised by tests, so it is an unused capability rather than dead code | `workers/runtime.py` |
| The reaper's advisory lock is session-level, taken on a pooled connection, and its result cached in a Python flag | The cached flag outlives the lock. When the connection holding it is recycled Postgres releases the lock, but the worker never rechecks, so it keeps reaping while a second worker can now take the same lock. Invisible at one worker | `workers/runtime.py`, `adapters/queue.py` |
| No `Idempotency-Key` on the approvals endpoint | A retried approval is refused rather than replayed; money does not move twice, but the message is wrong | `api/routes/approvals.py` |
| Orphaned objects are never reclaimed | Storage grows; documented as a deliberate trade with an unpaid cost | D-006 |
| Fixture credentials are committed | Local stack only, and marked as such — but they are in git history | `docker-compose.yml`, `.env.example` |
| Nothing is encrypted at rest | Deployment property | M10 |

The first three are the ones that matter. The rest are debts with owners.

---

## 6. How the claims here were tested

Two adversarial reviews were run against a scratch copy of this schema, with
agents given a real connection as the restricted role and told to assume the
application was fully compromised. A second set re-ran every finding rated high
or above, instructed to default to "does not reproduce".

That is why section C reads the way it does. The first version of the
least-privilege migration granted table-wide `UPDATE` on `payment`, and a
reviewer used it to point a pending payment at another vendor's bank account and
set the amount to 5,000,000 minor units. It reproduced exactly. Migration 0011
exists because of it.

The same review posted a balanced ledger transaction that referred to nothing,
and another with an invented kind, account and currency. Both committed.
Migration 0012 closed that.

Every control in the tables above is asserted by a test, and the tests
themselves are checked by mutation: each rule was weakened in the database or
the source, and required to make at least one test fail. The counts are recorded
with the decisions they belong to — D-034 for the payment triggers and the
grants, D-035 for the write policies, D-036 for the identity projection.

The policy run is the one worth reading. Its first pass killed 9 of 14, and the
five survivors were all real gaps rather than bad mutations — including a test
case labelled "one owner of two" whose two exceptions turned out to be owned by
the same role, so it had never tested what its name claimed. The comment
recording that is in `test_two_roles.py`, where the next person to touch the
case will see it.

### The document itself was reviewed, and it was wrong

Three reviewers checked every claim in this file against the code, with
instructions to flag anything false, unsupported or merely misleading. They
checked 117 claims and returned 20 problems. Most were citation errors — a
constraint named as `uq_document_content_sha256` when Postgres had auto-named it
`document_content_sha256_key`, an idempotency argument attributed to D-032 when
it lives in D-005 — and each one is the kind of thing that makes a document
unfalsifiable: a reader who greps for the cited name finds nothing and cannot
tell whether the control is missing or only mis-cited.

Three were worse than citation errors. The trust-boundary diagram had staff
presenting a bearer token to the API, which they never do, and left out the
console entirely — the one process holding every reviewer's token. The bound
claimed on the triage agent was the bound D-031 records as *failed*. And the
statement that auto-approval "requires matching a real PO and a real receipt"
was false in a way that turned out to matter.

**That last one found the most expensive defect in this repository.** Checking it
showed that an invoice whose extraction carried no line items skipped price,
quantity and receipt matching, raised nothing, and auto-approved — so an invoice
for 99,999.99 against a 220.80 order would have been paid with no person
involved. `skipped_checks` had recorded that nothing was compared since M4, and
nothing had ever read it. The matcher's own comment called that outcome "the
worst available"; the test covering it said the same thing in its docstring and
then asserted the behaviour that caused it.

It is fixed: a skipped check now raises `checks_incomplete` when the invoice
would otherwise have auto-approved. It was never reached in the demo data —
`skipped_checks` was empty on all 4,066 match results — which is exactly why it
survived four milestones.

The general lesson is the one this project keeps relearning: a value that is
declared and never read is not a control. That is now five instances.

---

## 7. What would change this document

- **A real model tier.** The containment is built and exercised on every commit
  by the deterministic tier — 13 red-team attacks, a read-only session proved by
  attempting writes through it — but never by a model. The first one changes the
  risk from argued to measured.
- **A real payment rail.** The sandbox cannot take money. A real one can take it
  and not answer, and the `SUBMITTING` state, the derived key and
  `uq_payment_authorisation` all start being load-bearing rather than tested.
- **More than one worker.** The reaper election weakness above is invisible at
  one worker and real at eight.
- **Multi-tenancy.** Every read is currently open to both runtime roles, which
  is correct for one company's ledger and wrong the moment there are two. D-035
  explains why read policies were rejected here; that reasoning would need
  redoing, not reusing.
