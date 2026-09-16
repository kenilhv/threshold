# Decision record

One entry per decision that would be expensive to reverse, or that a reader
would otherwise have to reconstruct from the code. Newest first.

---

## D-033 — The ledger balances because Postgres refuses it otherwise

**Date:** 2026-09-16 · **Status:** accepted

Double-entry is not chosen here for tradition. It makes one class of bug
arithmetically impossible: you cannot record a payment without recording where
the money came from, because the postings will not sum to zero.

That is only true if something enforces the sum. In most systems that something
is a service method everybody is supposed to call, which is a convention rather
than a guarantee — the first `INSERT` written by hand during an incident is the
one that breaks it.

### The trigger, and why it has to be deferred

`ck_ledger_entry_balanced` is a `DEFERRABLE INITIALLY DEFERRED` constraint
trigger. It runs at `COMMIT`, sums the entries of each affected transaction, and
raises `check_violation` if the total is non-zero.

Deferred is not an optimisation. Entries arrive one `INSERT` at a time, so the
set is *unbalanced between them* — a per-statement check would make correct code
impossible to write and would be worked around within a week.

It fires on `UPDATE` and `DELETE` as well as `INSERT`, and that is the half most
implementations miss. Adding a bad entry is the obvious attack on a ledger;
removing a good one from a transaction that already balanced is the quiet one.
`COALESCE(NEW.transaction_id, OLD.transaction_id)` is what lets one function
serve all three, because `NEW` is null on a delete and `OLD` is null on an
insert.

Verified against Postgres 16 before the migration was written, not after: a
balanced multi-row insert commits; an unbalanced one raises at `COMMIT`; a
`DELETE` and an `UPDATE` that unbalance a settled transaction each raise at
`COMMIT`. `tests/integration/test_ledger.py` re-runs all four through
SQLAlchemy so the failure arrives as an `IntegrityError` where the application
would see it.

`domain/ledger.py` validates the same rule independently. That is not
redundancy: the domain rule protects the path through the service and gives a
readable error at the point of the mistake, and the trigger protects the table
from everything else. Neither is sufficient and each catches the other failing.

### Integer minor units, and one conversion site

Amounts are signed `BIGINT` minor units. `Decimal` is right for tolerance
arithmetic — "how far apart are these two numbers" — and wrong for a ledger,
where the question is "do these cancel exactly". `BIGINT` rather than `INTEGER`
because cents overflow an `INTEGER` at about 21 million, which is not a large
invoice.

The conversion is `ledger.to_minor`, and it is the only one. It rounds
`ROUND_HALF_UP`, stated explicitly, because every `.quantize(SCALE)` elsewhere
in `src/` omits `rounding=` and inherits `ROUND_HALF_EVEN` from the ambient
context while the renderers use `ROUND_HALF_UP`. Half-even distributes error
better across many roundings, which is what a statistic wants; a supplier who
invoiced 10.005 and was paid 10.00 asks why, and "it rounds to even" is not an
answer about their money. In practice nothing should round at all, so this is
about which way a bug goes.

`MINOR_UNITS` lists only currencies this system has seen, and an unknown one is
a refusal rather than a guess at two — a default of two turns 1000 JPY into 10
JPY silently. There is a second exponent table in this repository,
`_ZERO_DECIMAL` in `seeds/layouts/multipage_credit.py`, and it is deliberately
not reused: its own comment says it drives formatting only, and it is right to,
because it lists HUF, which is printed without decimals and is formally a
two-decimal currency under ISO 4217. Reusing it to save five lines would have
introduced a real rounding error on Hungarian invoices.

Every stored amount carries the exponent it was written with, for the reason
`policy_version` is stored on `match_result` and `owner_role` on
`match_exception`: the table is a present-tense fact, and reading a historical
row through a corrected entry in it would move the decimal point.

### The gap this found

`match_result` and `approval_decision` between them carried **no monetary
column**. This schema could say an invoice was blocked and could not say for how
much; "how much is held up right now" was unanswerable without parsing a JSON
blob inside `extraction.fields`. That was not a deliberate omission, it was one
nobody had had a reason to notice, and a ledger has no way to proceed without
noticing it.

`match_result` now carries `amount_minor`, `currency` and `currency_exponent`,
nullable as a set — `ck_match_result_amount_is_whole` allows zero or three, never
one or two. Nullable because a match result whose total could not be read must
still exist; that is precisely the invoice that raised exceptions. The refusal to
pay against a null lives on the payment, which is where it belongs.

The currency comes from the page rather than from the purchase order.
`CURRENCY_MISMATCH` exists because those two can disagree, and what a supplier
billed is denominated in what they printed.

---

## D-032 — The payment executor is not a separate Go service

**Date:** 2026-09-16 · **Status:** accepted · **Reverses an M0 decision**

`ARCHITECTURE.md` said from M0 that there would be two deployable units, and
argued for a Go payment executor on the grounds that "the money path wants
transactional integrity, cheap concurrency and a small auditable surface, and it
is the one place a second language pays for itself".

Building the money path showed the argument to be backwards on its own strongest
point.

**Transactional integrity is the reason not to split.** It comes from Postgres,
and a process boundary between the approval and the payment is precisely what
takes it away. Two services cannot share a transaction. The payment row, the
ledger entries and the job that produced them commit together or not at all —
which is what makes a failed payment leave nothing behind and its retry collide
with nothing. Recovering that across a service boundary needs a distributed
protocol to get back what one `COMMIT` already gives, and every such protocol is
a new way to pay somebody twice.

**The auditable surface is a module boundary.** `domain/ledger.py` is 300 lines
with no database, no clock and no I/O. Putting it behind a network hop would not
make it smaller; it would make it harder to test and impossible to type-check
against its callers.

**The concurrency is a queue table.** `FOR UPDATE SKIP LOCKED` with leases and a
reaper, which this system has had since M3 and which the payment path reuses
unchanged. Payment volume in accounts payable is not a concurrency problem; it
is a correctness problem wearing a concurrency costume.

What a second language would actually have bought: a second toolchain, a second
test runner, a second CI lane, a second dependency graph, and a second place to
express the balance invariant — where the two expressions could disagree.

The honest summary is that the Go split was an architectural aspiration written
before the thing it described existed, and the act of building it is what made
the trade visible. It is recorded here rather than edited out of the
architecture document, because a reader deserves to know which claims survived
contact and which did not.

**What is genuinely lost.** A demonstration of a second language, which is a
real thing to want in a portfolio and not a reason to make a system worse. And
the blast-radius argument has some merit: a payment executor that cannot be
deployed independently cannot be rolled back independently either. That is
mitigated here by the payment path being a separate job kind with its own
handler and its own dead-letter queue, so it can be stopped without stopping
ingestion — but it is mitigation rather than isolation, and the difference is
worth naming.

---

## D-031 — The agent is not the router, and that is what bounds an injection

**Date:** 2026-09-16 · **Status:** accepted

The first version of the escalation packet bounded `owner` to the four members
of `OwnerRole`. That reads like a control and is not one. It bounds the
*alphabet* a captured model may spell in, not the *word* it chooses, and the
attack that matters needs no illegal characters: a payment redirect addressed to
an accounts-payable clerk is a real role, a real code, correctly spelled, and
past every check.

The red-team suite was written to test the boundary and found the boundary was
wrong. That is recorded here rather than quietly fixed because the sequence is
the argument: a suite that only confirms the defences you already believed in is
a suite you chose.

### What replaced it

Routing already happened before any agent runs. `match_exception` rows carry
`owner_role` and `due_at`, stamped from the frozen taxonomy at the moment the
control fired, and a partial index makes them a role-scoped, deadline-ordered
worklist. Every exception this system can raise is on a named desk with a clock
on it whether or not an agent ever speaks.

So the agent is not deciding who is responsible. It is saying which of the
people *already involved* should look first. Two rules make that structural:

* **Grounding.** Every cited code must be one recorded against this match
  result. The agent cannot invent an exception or import one from another
  invoice.
* **Implication.** The owner must be the recorded owner of at least one **cited**
  code.

The second is phrased against the cited codes and not against the invoice, and
the distinction is the whole of it. On an invoice carrying both a redirect and a
price query, the buyer *is* implicated — so an invoice-level rule cheerfully
routes the bank change to them. `misroute-to-an-owner-the-invoice-does-implicate`
is the attack that forced the wording, and
`test_the_owner_must_match_a_cited_code_and_not_merely_a_recorded_one` is what
holds it.

The blast radius of a completely successful injection is now: reorder the
priority of people already going to be involved, and write them a sentence.

### The evidence is the row, not the taxonomy

`OpenException` carries the owner written when the exception was raised.
`spec_for` has exactly one caller in `src/` — inside `_raise` — and this is not
a second one. Re-deriving ownership at validation time would answer "who owns
this code today" when the question is "was this escalation legitimate at the
time", and `models.py` already argues that case for the column itself.

It is also the type `domain/approval.py` already used to decide whether a
*human* may approve, loaded by the same `open_exceptions`. An escalation and an
approval are two things done about one set of open exceptions; a parallel notion
of "what is open" would be a second thing to keep in step.

### What this does not fix

Nothing here touches the summary. It is free text, written by a model that read
the supplier's own line descriptions, and no validation escapes natural
language. It is capped at 600 characters, held in `AgentText` so an f-string
cannot leak it, and rendered by the console quoted and attributed. Within that
it can still persuade the person who reads it. That is named in
`TestTheAttacksThatSurvive` with a test each, rather than described as handled.

---

## D-030 — The agent's evidence is fetched up front, through the tool registry

**Date:** 2026-09-16 · **Status:** accepted

`Triager.triage` receives the invoice already read, rather than a session to
read it with. There is no tool-calling loop.

The agent's readable surface for one invoice is four rows — the match result,
its exceptions, the document record and the purchase order — and there is
nothing to choose between them. A loop over four known rows buys no selectivity
and costs a class of failures worth avoiding: partial reads, loops that do not
terminate, a model that decides not to look at the exceptions. Every one of
those is a failure mode with no upside here.

The tools are not bypassed. `services/triage.py` builds the request by calling
`tools.resolve("get_match")` and the other three, through a session opened
`SET TRANSACTION READ ONLY`. That matters for a reason unrelated to elegance:
it makes the registry the production path. `test_agent_boundary.py` asserts
things about `TOOLS` — that every entry is a read, that `escalate` is not in it
— and those assertions are worth something only while something runs them.

A model tier that genuinely needs multi-turn retrieval will need this revisited.
That would be a real trade, and the thing being traded away is written down
here so the trade is visible when somebody makes it.

### Two sessions, and the reason there are two

The evidence is read inside `tools.read_only`. The escalation is written into
the *caller's* session, which is a different connection, and only after the
read-only transaction has closed.

That asymmetry is deliberate in both directions. The connection the agent's data
arrived on is provably incapable of carrying its output back — the usual shape
of that bug is one session threaded through a pipeline until some helper deep
inside it flushes, and here there is nothing to catch because the readable
session is never in scope where the write is. And the write belongs in the
caller's transaction, so a worker job that fails after escalating rolls the
escalation back; otherwise its retry collides with
`uq_escalation_match_result` and a transient failure becomes a permanent one.

---

## D-029 — The deterministic triage tier ships first, with an assembled summary

**Date:** 2026-09-16 · **Status:** accepted

`ARCHITECTURE.md` has said since M0 that the model tier arrives at M7. What
arrives at M7 is the *boundary*, the persistence, the audit trail, the console
rendering — and a tier with no model in it.

The tier sorts: worst severity, then the tightest deadline, then the code's own
name so the result is stable. On an invoice carrying a payment redirect and a
price query it leads with the redirect, because four hours beats forty-eight and
both block. That is the entire judgment.

It escalates only when more than one desk is implicated, and returns `None`
otherwise. An invoice whose exceptions all belong to one owner is already on
that owner's queue, ordered by the deadline stamped when the control fired; an
escalation there restates the exception list in different words and adds a row
that looks like a finding. `None` is an answer, and on most invoices it is the
right one.

### Why this order and not the other

Its summary is assembled from the frozen taxonomy, so no supplier-controlled
text reaches the sentence a reviewer reads. A model tier reading the same
request has read the invoice's own prose and will not be able to claim that.

So shipping this first is not a placeholder. It means the escalation path, the
validation, the persistence, the audit attribution and the console all get
exercised — in CI, hermetically, on every commit — by something that cannot be
captured. When a model tier arrives, the only new variable is the model. The
alternative order gets a system whose failures are ambiguous on day one.

The same argument produced `adapters/extraction/` at M2 and is carried over
deliberately.

### The escalation is its own table

Not three columns on `match_result`. Columns on the decision row read as part of
the decision, and six months from now somebody joins them into a report that
treats an agent's opinion as a system finding. A foreign key with a different
name does not invite that.

`ck_escalation_cites_something` repeats the domain rule at the database.
`domain/triage.py` protects the path through the agent; the constraint protects
the table.

`actor_type = 'agent'` has been legal in `ActorType` and in the audit table's
check constraint since M0 with nothing producing one. It has a producer now.
"The system did this" and "an agent suggested this" are different claims, and an
audit trail that merged them would make the second unfalsifiable in the one
record built to answer that question. The audit row also carries which recorded
codes went *uncited* — the only durable trace an agent that stayed quiet about
something leaves behind.

---

## D-028 — The evaluation defends against being vacuous, not against being wrong

**Date:** 2026-09-12 · **Status:** accepted · **Has a known, unpaid cost**

M6 built an evaluation harness, derived a threshold from it, and put a
regression gate in CI. This entry records what that machinery is *for*, and —
more usefully — the one thing it cannot do, so that nobody later mistakes a
green gate for a claim about production.

### The corpus is its own answer key

Every document is rendered from an `InvoiceSpec`, and that spec is the ground
truth. There is no labelling step and no transcription, so the answer cannot
drift from the page. That is worth more than it sounds: a hand-labelled corpus
decays silently as documents are regenerated, and nobody notices until the
numbers stop meaning anything.

The price is that ground truth must be *printable*. A spec carrying
`244615.0647` describes a document that cannot exist, because an invoice prints
two decimals — and scoring against it marks a perfectly correct reading of
`244615.06` as wrong. That was a real bug and it cost twelve points of apparent
accuracy on `total` before anyone checked which of the scorer and the extractor
was at fault. The corpus now generates money at the precision a page can print
and a test walks every value asserting it.

### Three ways the evaluation could stop evaluating

Each of these produces a clean, plausible, higher number, because removing
readings removes the hard ones first.

**A corpus that shrank, or lost a layout, or stopped rendering.** The harness
refuses to return: it checks the reading count, the document count and the
layout coverage before anything is reported.

**A corpus with no errors in it.** A run in which everything is read correctly
calibrates nothing — every bucket is 100%, the curve is a single point, and the
derived threshold is wherever the code happened to land. The harness refuses
that too, and a unit test asserts the corpus still contains a confidence value
that is neither always right nor always wrong, because without one there is no
boundary to find.

**A scorer that quietly stopped scoring.** `score()` raises if
`decision_relevant_fields` no longer provides a field it expects, rather than
measuring four out of five and reporting a better number.

The regression gate runs all of these *before* any comparison against the
baseline. A gate that compared first could be green against a baseline
regenerated from an evaluation that had already stopped working.

### The cost, stated plainly

**None of that defends against the corpus being unrepresentative.** The
documents are synthetic, and the same hand wrote the generator and the
extractor. The evaluation can verify that the extractor reads what reportlab
wrote. It cannot verify that what reportlab wrote resembles an invoice a real
accounts-payable function receives, and the honest expectation is "partly".

So the claim M6 supports is narrow and should be quoted narrowly: *on documents
of this kind, invoices above the gate are read correctly at a rate whose 99%
lower bound is 0.9941*. It is not a production accuracy figure, and a reader who
takes it as one has been misled by the rigour of everything around it — which is
exactly the failure mode a careful evaluation invites. Precision about the wrong
population is still wrong, and it is more persuasive than being vague.

The remedy is not more engineering. It is a few dozen real invoices with a
human-made answer key, held out from everything, scored by the same harness. A
single day's work, blocked only on having the documents. Until then this is
measured confidence in a controlled setting rather than evidence about the
world, and the difference matters most precisely when the numbers look good.

### What the gate does not cover

It scores the five fields that gate a decision, which is deliberately not every
field the extractor reads. Payment details are the notable omission:
`decision_relevant_fields` excludes them because an unreadable bank line scores
0.0 and would trip the confidence gate on every invoice that prints no payment
block, so that signal has its own path through the exception layer (D-016). A
change to bank-detail extraction is therefore invisible to `make eval-check`,
and is covered by the matching tests instead.

That boundary was found by mutating bank-detail extraction and watching the gate
stay green. It is a coverage boundary rather than a hole — but only because it
is written down, and it is recorded here because the next person to read
`check_eval.py` would reasonably assume it covers everything the extractor does.

---

## D-027 — The auto-approval gate, re-derived from a measured curve

**Date:** 2026-09-12 · **Status:** accepted · **Closes D-014**

D-014 set this task three milestones ago and set its terms: the gate is 0.50,
nobody validated it, and it "has to be re-derived from the calibration curve
rather than tuned until the demo looks good. Tuning it against a fixture is how
a control becomes a decoration."

It is now **0.60**, and the policy version moved to `2026.09.2` because a
tolerance is stamped on every decision and changing one is an event with a name.

### What was measured

Four hundred synthetic invoices for the regression gate, three thousand for this
derivation, rendered across four layouts from specs that are their own answer
key. `make eval` reproduces it. Over 3000 documents the gate's own input takes
four values:

| gate confidence | documents | read correctly | pooled lower bound |
| --- | --- | --- | --- |
| 0.616 | 456 | **456 (100%)** | 0.9941 |
| 0.580 | 12 | **0 (0%)** | 0.9593 |
| 0.434 | 282 | 59 (20.9%) | 0.6582 |
| 0.000 | 2250 | 0 (0%) | 0.1606 |

So the evidence identifies an **interval**, not a point. Every threshold in
(0.580, 0.616] accepts exactly the same invoices and carries exactly the same
measured risk: 456 accepted, none wrong, and a 99% lower bound of 0.9941 on the
accuracy of everything let through.

0.60 rather than 0.616 because the interval is what was measured and a point
inside it is what should be written down. 0.616 is an artefact of this
extractor's arithmetic — a date found on the line after its label scores
0.88 × 0.7 — so a threshold pinned to it would flip on a change that altered no
evidence at all.

**The old 0.50 was not merely unvalidated. It was wrong.** It sat below 0.580,
so it admitted a bucket in which every single document was misread. On the
400-document corpus that is three wrong auto-approvals out of fifty-six, a 5.4%
false-approval rate. Each one is an invoice paid on a misreading. The correction
tightens a control, which is the direction that needed no courage; what it cost
was finding out.

### Four ways to derive a confident wrong number, and what was done instead

**A gate selects a set, not a bucket.** The obvious rule — take the lowest
confidence whose own accuracy clears the target — is wrong, because risk belongs
to everything accepted rather than to the marginal bucket. On the field-level
curve it puts the gate at 0.93 and escalates 56 readings that were all correct,
buying nothing. The statistic is the pooled accuracy of the accepted set.

**A percentage without a sample size is not evidence.** A sixty-document probe
had a bucket of two readings at "100% correct"; at four hundred documents that
same bucket is 60%. The normal approximation would have called the first one
certain — its half-width is zero when every reading is correct — so every bound
here is Wilson and the gate reads the lower one.

**Binning is a free parameter unless the value is the bin.** Expected
calibration error on this data ranges from 0.102 to 0.196 by binning alone, with
no datum changing, and coarsening can only shrink it. One bin per distinct
confidence value removes the choice and is the maximum over all binnings. ECE is
reported and is deliberately not the headline: it is dominated by readings the
gate never accepts.

**Calibrate the gate the system has, not a proxy for it.** Two versions of this
harness produced complete, plausible reports about a gate that does not exist.
The first calibrated *field* accuracy, when the gate takes a minimum across five
fields and approves an *invoice* — a strictly more forgiving question. The
second calibrated `lowest_decision_confidence`, when the matcher compares
something else: it drops `vendor_name` once the vendor resolves, on D-014's own
corroboration argument. Since the extractor scores `vendor_name` 0.45 by
construction, that harness reported the gate auto-approving nothing whatsoever.

### What this number does not mean

The corpus is synthetic and the same hand wrote the generator and the extractor.
That bounds the claim, and the bound is not small.

**It is not a production accuracy figure.** It says how this extractor behaves
on documents of this kind. Whether documents of this kind resemble the invoices
a real accounts-payable function receives is a question no amount of care inside
the harness can answer, and the answer is almost certainly "partly".

**It is not evidence the confidence scale means anything.** It still does not.
The signal ranks at AUC 0.99 and calibrates at ECE 0.19, which is exactly what an
ordinal score wearing probability clothing looks like: 0.45 does not mean 45%, it
means "more likely right than 0.43". The gate does not need it to mean 45% — it
asks only how often the invoices above the line are read correctly, which is
measurable whether or not the scale is a probability. That is the narrow claim
being made and it is the only one the data supports.

**Coverage is 15%, and that is the extractor's problem rather than the gate's.**
Only about one invoice in six clears the bar, because this tier reads `total`
and `issue_date` well on one of four layouts and poorly on the rest. Lowering
the gate to raise coverage would buy it entirely with wrong payments: the next
bucket down is 0% correct. The way to raise coverage is a better extractor,
which is what the model tier in M7 is for, and the evaluation is now in place to
say whether it helps rather than to assume it.

### Still unpaid

There is no held-out set of real documents. Every self-check in `make eval`
defends against the evaluation being *vacuous* — a corpus that silently shrank,
a scorer that stopped scoring, a curve with no uncertain bucket to find a
boundary in. None of them defends against it being *unrepresentative*, and no
CI engineering substitutes for a few dozen real invoices with a human-made
answer key. That is the honest gap, and it is the same shape as the one D-014
named: a number can be derived rigorously from the wrong population.

---

## D-026 — Nine alert rules, each proved to fire and proved to stay quiet

**Date:** 2026-09-11 · **Status:** accepted

Alerting does not die from a missed page. It dies from a rule that fires during
normal operation until people stop reading it, and by then the rules that would
have mattered are in the same folder as the one that cried wolf. So every rule
here appears twice in `alerts_test.yml`: once with input that must make it fire,
and once with input that must not — and the negative case is deliberately close
to the positive one. A queue that is busy rather than stuck. An API with a real
but acceptable error rate. An invoice mix that is worse than usual but still
automating. For `OldestJobStuck`, the exact worst case the design is allowed to
produce: three minutes, being two backoffs plus a crash-and-reap cycle, which
must not wake anyone.

The tests run under Prometheus's own `promtool` against the pinned image that
serves the stack, because the rules are PromQL and only Prometheus can say what
PromQL means. `make verify` runs them.

**Two of the negative tests were passing for the wrong reason**, and mutation
testing is what found it. `ApiErrorRate` and `AutoApprovalRateCollapsed` each
carry a denominator guard — a floor on request rate, a floor on decision volume
— without which they page every quiet night about arithmetic on a near-zero
divided by a near-zero. Both negative tests used flat counters, so the numerator
was zero as well, the ratio was `0/0`, and no alert fired for reasons entirely
unrelated to the guard. Deleting the guard changed nothing and the suite stayed
green. The inputs now move: an API where *every* request is a 5xx at one request
a minute, and a night with twelve invoices and no auto-approvals. Both tests
fail without the guard, which is what they were always supposed to assert.

Seven mutations were applied and watched to fail before the rules were kept,
including a threshold moved until the healthy case pages, a ratio quietly
rewritten as a rate, and a runbook link pointed at a file that does not exist.

**`make demo-alert` changed a rule.** The demo stops the worker and watches
`QueueObserverStopped`, and the first run showed it reaching `pending` at
t+337s rather than immediately. The reason is that a *pushed* series does not
become absent when the pusher stops — it becomes absent when Prometheus's
five-minute lookback passes the last sample. The expression was therefore
already carrying five minutes of hysteresis, and the rule's own `for: 5m` on top
made the true detection latency for a dead worker **ten minutes**. It is now
`for: 2m`, so a page arrives about seven minutes after the pool dies, and the
arithmetic is written next to the rule. Nothing but running it would have shown
this: the rule was correct, its tests passed, and the number that mattered was
the sum of two waits in different systems.

Every rule links to a runbook and a test asserts the file exists, because a dead
link is discovered by the person least able to cope with it — someone paged at
3am, following the link precisely because they do not already know the answer.
Six runbooks serve the nine alerts, each opening with the alerts it covers, what
normal looks like in measured numbers, how to tell the cases apart, and a
"what not to do" section. Two of those matter more than the diagnosis: do not
clear the queue, and do not widen tolerances to silence
`AutoApprovalRateCollapsed`, which converts a visible problem into invoices
clearing that should not have.

The registry now closes both loops. `MetricSpec` names the dashboard panels and
the alert rules that read each metric, and tests assert in both directions:
every declared consumer exists, and every panel and every rule is claimed by a
metric. An alert nobody's metric claims evaluates forever without firing and
looks exactly like a rule that is working.

There is **no Alertmanager**. Routing and notification are a separate concern
with separate failure modes, and a local Alertmanager delivering to nowhere
would prove nothing that Prometheus's own `/api/v1/alerts` does not. What was
worth building is the rules and the evidence that they are right.

---

## D-025 — Readiness checks what the service uses, and never the identity provider

**Date:** 2026-09-11 · **Status:** accepted · **Fixed**

`/readyz` checked Postgres and Redis. Nothing in this application has ever used
Redis. MinIO and Keycloak are used by every upload and every authenticated
route, and neither was checked — so with only Keycloak down the API reported
itself ready while every authenticated call returned 503. D-017 recorded this as
M5's to fix and asked the question that matters: whether a shared identity
provider belongs in readiness at all.

It does not, and the reasoning is the substance of this entry.

**Readiness is not a status page.** It answers one question: should a load
balancer send this instance a request. Every dependency it checks is a
dependency that can pull the instance out of rotation. An identity provider is
shared, so checking it fails readiness on every instance of every service at the
same instant, and there is nowhere left to route. The probe then causes an
outage strictly worse than the fault it is reporting — a degraded system becomes
a total one.

**It would also be false.** Signing keys are cached per instance, so a server
that has already fetched them keeps validating tokens perfectly well while
Keycloak is unreachable. Marking it unready removes a working server.

So identity is reported and never gates routing: how many keys this instance
holds and how long ago it learned them. That is the number an operator wants
during an IdP incident, because it distinguishes the instances that can still
authenticate from the ones that will start returning 503 on their next unknown
key. The report reads cached state and never calls Keycloak — compose polls
`/readyz` every five seconds for ever, and a readiness probe that fetches JWKS
is a denial of service against the IdP, aimed by the thing meant to be watching
it. A test asserts the probe never calls out.

What *is* required is Postgres and the object store. The object store because
D-006 stores the bytes before writing the row that describes them: an instance
whose bucket is unreachable cannot accept a document at all. The check is a
`HEAD` against the bucket rather than `exists()` on a key, because `head_object`
returns the same 404 for a missing key and a missing bucket — fine for the
question `exists` asks, useless here, where one means every upload fails and the
other means nothing.

`required` is on the wire rather than implied, so the answer to "why is this
instance ready when something in its list is not ok" is in the response instead
of in someone's memory of the module.

The endpoint had **no tests at all**, which is how the wrong dependency list
survived four milestones. It has twelve now, and five mutations were applied and
watched to fail before they were kept: making identity required, ignoring
`required` when computing readiness, dropping the object store check, marking
every dependency optional, and giving liveness a database call.

**Redis is gone**, not merely unchecked: the container, the dependency in
`pyproject.toml`, the setting, the compose service and both `depends_on` blocks,
the Makefile's migration target, four test fixtures, the CI comment and the
cloud-mapping tables. It was in the stack from M0 on the strength of a plan —
idempotency cache, locks, rate limits — and none of it was ever built, because
idempotency landed in Postgres where it could share a transaction with the work
it protects, and the queue's locking is `SELECT … FOR UPDATE SKIP LOCKED`. A
dependency kept for a future that already resolved differently is a dependency
to remove. `api` and `worker` now wait on `minio-init` instead, which is what
they actually need: the bucket, not merely the server.

---

## D-024 — Metrics are pushed to Prometheus over OTLP, and there is no collector

**Date:** 2026-09-11 · **Status:** accepted

Prometheus scrapes. This pushes, and the reason is the worker: it has no HTTP
server, and a scrape model means either giving it one purely so something can
scrape it, or running a collector to scrape on its behalf. Prometheus 3's OTLP
receiver removes both, so the application exports the same way to both backends
and nothing exists whose only job is forwarding.

A collector earns its place when there is fan-out, sampling, or a backend to
insulate against. There is none of that on one host, and the seam survives
anyway: the endpoints are configuration, so pointing the services at a collector
later changes no code.

The cost is paid in what push does not give you. There is no `up` series and no
scrape staleness, so "the worker stopped reporting" cannot be detected by a
target going down — a pushed series stays queryable for the five-minute lookback
and then simply stops answering. The alerts in M5's last increment have to use
`absent()` rather than waiting for a series to vanish. The second cost is that
two processes exporting on their own timers have no shared clock, so
`out_of_order_time_window` has to be set or a sample landing slightly late is
rejected; thirty minutes is far more slack than a fifteen-second interval needs.

Three things had to be measured rather than assumed, and all three are the kind
that fail silently.

**`service.instance.id` must be set, and must be stable.** Prometheus maps it to
the `instance` label. The Python SDK's `Resource.create()` invents a random UUID
when nothing supplies one, so every restart would abandon every series and start
a new one — and without it entirely, two replicas with the same `service.name`
are not rejected, not logged, and not counted twice: Prometheus merges them into
a single series and interleaves both processes' samples, so one counter reads
120 while the two processes independently counted 12 and 120. `resource.py` sets
it to the hostname, which in a container is the container id: stable for the
life of the reporter, distinct per replica, and deliberately not
`worker_identity()`, which carries a pid and a random suffix because it
identifies a *claim holder* rather than a reporter.

**Resource attributes are not labels.** `deployment.environment` exists only on
`target_info` unless it is named in `promote_resource_attributes`, so
`sum by (deployment_environment)` returns nothing at all rather than failing.

**The translated names are not guessable.** Prometheus rewrites an OTLP
instrument name in Go: dots become underscores, a counter gains `_total`, a unit
of seconds appends `_seconds` — but a unit whose word already appears in the
name appends nothing, which is why `db.client.connections.usage` becomes
`db_client_connections_usage` and not `..._connections`, and an annotation unit
like `{job}` appends nothing either. A wrong guess produces an empty Grafana
panel and no error anywhere. So each `MetricSpec` records the name it expects
and `test_telemetry_stack.py` checks all eleven against a running Prometheus.

That check is one half of a pair. `observability/metrics.py` also declares which
dashboard panels plot each metric, and `test_dashboard.py` asserts in both
directions: every panel a metric names exists in the provisioned JSON, and every
metric a panel queries exists in the registry. Neither half alone is enough —
the first permits a panel querying a metric nobody declared, the second permits
a metric naming a panel that was deleted. Seven mutations were applied and
watched to fail before the tests were kept, including renaming a panel,
misspelling a metric, and grouping by a label that does not exist.

A related decision, small but load-bearing: **the oldest-claimable-job gauge
reports zero for an empty queue rather than reporting nothing.** The first
version omitted it, on the argument that zero is indistinguishable from "a job
arrived this instant". That is true and it does not matter — both mean nothing
is behind. What matters is the other reading. If an empty queue and a dead
observer are both absence, then the alert for a dead observer cannot be written.
Nothing waiting is a measurement; nothing reporting is an outage, and the two
must not look the same.

Grafana runs with anonymous access and no login form, which is **local only**
and is the single riskiest line in `docker-compose.yml`. It is there because a
password prompt between a stranger and the point of this project is a worse
trade on a laptop; anything reachable beyond this host drops those four
variables and puts real authentication in front. `GF_AUTH_BASIC_ENABLED=false`
stays either way — that one is hardening rather than relaxation.

---

## D-023 — SQLAlchemy is instrumented on the engine, not on the module

**Date:** 2026-09-11 · **Status:** accepted · **Fixed**

`configure_tracing` called `SQLAlchemyInstrumentor().instrument()` with no
engine, on the argument — written into the code as a comment — that instrumenting
globally saved `init_engine` from having to know about tracing. That call
reached nothing, and had reached nothing since the day it was added.

The global form works by replacing `create_async_engine` on
`sqlalchemy.ext.asyncio`. `adapters/db.py` binds that name with a
`from … import` at module scope, and `api/main.py` imports `adapters/db` at
module scope, so the name is resolved when the API module is imported — long
before `create_app()` runs anything. `db.py` therefore held the original
function for the life of the process, and every engine it built was untouched.

Measured, in the production import order:

    >>> import threshold.api.main
    >>> db.create_async_engine is sqlalchemy.ext.asyncio.create_async_engine
    False

The consequences were entirely silent. The service emitted a `connect` span and
no `select` spans at all, so the SQL half of every trace M5 exists to produce was
missing; and `db.client.connections.usage` — declared in the metric registry —
was never produced. Nothing raised, nothing logged, and the only evidence was an
absence in a backend that had not been stood up yet. It was found by writing a
test that asserted a declared metric actually appears.

The fix instruments the object rather than the factory:
`SQLAlchemyInstrumentor().instrument(engine=_engine.sync_engine)` inside
`init_engine`, where the engine exists. That cannot fail this way whatever the
import order is. The cost is that `adapters/db.py` now imports from
OpenTelemetry, which is the trade the original comment declined; it is worth it,
because the alternative is a comment explaining why an import was avoided
attached to code that did nothing.

Botocore stays global and is not affected. It wraps
`BaseClient._make_api_call` on `botocore.client`, which is a method on a class —
every holder of the class sees the patch, whenever it was applied. Verified by
reading the instrumentor rather than by analogy.

`TestTheInstrumentationReachesTheEngine` pins both signals, and fails with
"spans were []" when the fix is reverted.

---

## D-022 — Metric labels are a schema the SDK enforces

**Date:** 2026-09-11 · **Status:** accepted

Cardinality is what kills a metrics backend, and it dies to a single attribute
that looks entirely reasonable where it is written. One `document_id` on one
counter turns a six-series metric into a series per invoice, permanently, and
nothing fails at the time — not the request, not the test suite, not the build.
Review is a weak control for a defect whose signature is a line that reads
correctly.

So every instrument is declared in `observability/metrics.py:REGISTRY` with the
exact attribute keys it may carry, and `configure_metrics` compiles each
declaration into an SDK `View` with `attribute_keys=`. An attribute nobody
declared is removed by the SDK before an exporter sees it. The registry is
enforcement rather than documentation, which is the whole point: a call site
cannot leak a label by being careless, only by editing the registry.

`FORBIDDEN_LABELS` guards that other direction, and `test_the_check_can_fail`
guards the guard. Every check added here was reverted deliberately and watched
to fail before being kept; nine of them, including "the views stop enforcing the
allowlist" and "the job kind clamp is removed". A label test that passes because
it checks nothing is worse than no label test, and this suite has shipped that
mistake three times.

Each entry also names what reads it, and a declaration with an empty `read_by`
fails the suite. The recurring defect in this repository is "declared and never
read" — `audit_event.trace_id` held the wrong value for three milestones,
`DeadLetter.dedupe_key` has never been written — and a metric nobody plots is
the same defect in a different costume. D-023 exists because that check caught
one on the day it was written.

Four decisions inside this were measured rather than assumed, and each changed
the shape of the code:

**Histogram buckets are explicit.** The SDK's defaults are `(0, 5, 10, 25 …
10000)`, which are millisecond-scaled. A histogram declared in seconds that
accepts them puts every number this system produces into the `le=5` bucket: job
execution measured 3–52 ms over 317 jobs, so the metric would be named
`_seconds`, scrape cleanly, and be able to report only "under five seconds". The
boundaries here come from measured distributions, and the queue-wait boundaries
sit on the points where the system's behaviour changes — the one-second poll,
the five- and ten-second backoffs, the two-minute lease — because "waited longer
than a poll" and "waited longer than a lease" are different incidents with
different runbooks.

**Queue depth is an observable gauge fed by a snapshot.** A synchronous `Gauge`
disappears from an exposition on any scrape where `set()` was not called since
the previous one, so a depth sampled every 30 s and scraped every 15 s would
flap in and out of existence. Observable gauges re-run their callback on every
collection. But those callbacks run on the exporter's thread — and on the
caller's thread during `force_flush` — where there is no event loop to await a
database on, so the worker takes the reading on its own loop and the callback is
a pure read of the result.

**The snapshot expires after 90 seconds.** A frozen gauge is worse than an absent
one: it reports a healthy queue for ever after the observer dies, which is how
people learn not to trust a dashboard. An absent series is a question Prometheus
can ask with `absent()`.

**Exactly one worker publishes it.** Queue depth is a property of the queue, so
N workers reporting it would make `sum()` report N times the truth. The reaper's
advisory lock already elects one worker for the same reason, and reusing it
means there is one answer to "who speaks for the queue" instead of two
mechanisms that can disagree.

The API's RED metrics are deliberately **not** hand-rolled; the FastAPI
instrumentation already emits them, and a second set would double-count and
disagree about labels. Two of its properties were verified rather than trusted,
because they are the ones that decide whether this is safe to expose: the route
arrives as a template, so `/v1/documents/{document_id}/match` is one series
rather than one per invoice, and a request matching no route carries no route
attribute at all, so a port scanner cannot grow the exposition. The trap is that
the template arrives under `http.target`, which is the same key that holds the
*raw* path on spans.

---

## D-021 — A job continues its producer's trace rather than linking to it

**Date:** 2026-09-11 · **Status:** accepted

The messaging convention is that a consumer *links* to the producer's span
rather than becoming its child, because a message may be consumed arbitrarily
later and a span that stays open across that window misrepresents the work.
This system does the opposite for the automatic chain, and the reason is that
"arbitrarily later" is not true here — it is bounded, and the bound is small
enough to be measured rather than assumed.

The bounds are: three attempts including the first, five then ten seconds of
backoff between them, a one-second poll, and a two-minute lease with a
thirty-second reaper. Measured against 317 completed jobs in the local stack,
the wait between a job becoming available and being claimed is:

| kind | p50 | p95 | max |
| --- | --- | --- | --- |
| `extract_document` | 0.503 s | 0.944 s | 1.084 s |
| `match_invoice` | 0.029 s | 0.131 s | 0.240 s |

`extract_document` is uniform across the one-second poll because the producer
commits at an arbitrary point in the polling cycle; `match_invoice` is an order
of magnitude faster because the worker enqueues it from inside the extract
handler and its loop claims again immediately rather than sleeping. The
worst case inside the design — two failures with full backoff, or a
crash-and-reap cycle — is tens of seconds to a few minutes.

Against that, linking costs something concrete. `ARCHITECTURE.md` has promised
since M0 that `audit_event.trace_id` is "the same id as the distributed trace".
One invoice writes four audit rows across two processes and two queue hops. If
each hop started its own trace, that promise would resolve to four different
ids for one invoice, and the query that milestone exists to enable — show me
everything that happened to this document — would need a join through the links
instead of an equality. Continuing the trace is what makes it an equality.

The trade is only valid while those bounds hold. A longer backoff, a larger
attempt budget, or jobs scheduled hours ahead invalidate it, and it must then be
decided again rather than inherited — which is why this is written down instead
of living only in the shape of the code.

A replayed dead letter is the exception and does start its own trace, carrying a
span link back to the one that died. A replay is a human decision taken possibly
days after the failure; inheriting that trace would make today's work appear
inside last week's and report the retry as a six-day operation. `replay()`
moves `_traceparent` to `_linked_traceparent` for exactly this, and
`test_job_trace_context.py` pins both halves.

---

## D-020 — The audit log stores the trace id, and does not store the request id

**Date:** 2026-09-11 · **Status:** accepted · **Fixed**

`audit_event.trace_id` held the request id. `ARCHITECTURE.md` had said since M0
that the column is "the same id as the distributed trace", so the sentence was
false in two different ways at once: the value was the wrong identifier, and
every row the worker wrote was `NULL` because a background job has no request.

The two identifiers are not interchangeable and neither is a substitute for the
other. A request id is chosen by the caller — `smoke-test-id` is one this
repository really sends — so it is not, and cannot be made to be, the 32
lowercase hex characters a W3C trace id is. It is a handle a caller can quote
back when asking about their own call. A trace id is generated by the SDK,
survives process boundaries, and is the thing a trace backend indexes.

Storing both was considered and rejected. The audit table is the durable record
of what happened, and the durable identifier for "what happened" is the trace
id; a request id is a handle for one conversation with one client, and its
natural home is the log line, where it already sits beside the trace id on every
line of that request. That adjacency is the join: a caller quotes a request id,
the logs turn it into a trace id, and the trace id reaches both the audit rows
and the spans. A second column would duplicate that join into the schema and
would be `NULL` for every row written by a worker, which is most of them.

The value now comes from the active span, via
`observability/tracing.current_trace_id()`, which returns `None` outside a
trace rather than formatting an invalid span context — a naive format yields
`00000000000000000000000000000000`, which is indistinguishable from a real id
in a column and would make "this row has no trace" unaskable.

`tests/integration/test_trace_continuity.py` reaches the rows by joining through
the entities they describe rather than selecting on `trace_id`, because
selecting rows by trace id and then asserting their trace ids match is circular.

---

## D-019 — The queue trusted the worker's clock

**Date:** 2026-09-10 · **Status:** accepted · **Fixed**

`models.py` has said since M0 that timestamps "are set by the database, not the
app, so two workers on skewed clocks cannot disagree about ordering." The queue
did not honour it. `claim()` computed `lease_expires_at` from the worker's own
clock, `fail()` computed the retry `run_after` the same way, and both were then
compared against the database's `now()` — in the claim predicate and in the
reaper. Every lease was only as good as the agreement between two clocks.

That agreement is not something to assume on this stack. The Docker VM had just
come back from the host sleeping, and WSL2 clocks are known to drift after
resume. A worker running a few minutes behind would write leases that were
already expired as far as the database was concerned; the reaper would return
live jobs to `pending` and a second worker would run them. That is double
processing, in a system whose next milestone moves money.

It surfaced as a test that passed alone and failed in the full run.
`test_attempts_are_exhausted_then_dead_lettered` wrote the host's `now()` into
`run_after` and claimed in the same transaction, where Postgres's `now()` is the
transaction start — so it passed or failed on sub-millisecond skew, depending on
load. The measured skew at the time was about a millisecond. Fixing the test
alone would have made the gate green and left production with the same defect,
so the fix is in the queue: every timestamp it writes and later compares —
`claimed_at`, `lease_expires_at` on claim and heartbeat, the retry `run_after`,
`replayed_at`, the purge cutoff — now comes from `func.now()`. The module has no
client-clock timestamps left. `fail()` returns the stored retry time rather than
logging its own estimate of it.

`TestClockSkew` pins it by making the queue module's clock run ten minutes slow.
With the old code the reaper takes the live job; verified by reverting the one
line and watching it fail. With the fix the lease holds, and a retry cannot be
scheduled into the past.

The general point is the same one D-015 made about stored values: a guarantee
written in a docstring is not a guarantee until something breaks when it is
violated.

---

## D-018 — The API answered before it committed

**Date:** 2026-09-10 · **Status:** accepted · **Fixed**

Found while checking the console, not while writing the API, which is the
uncomfortable part. Approving an invoice and reading its decisions back in the
same breath came back empty in **6 of 8 trials**, and populated half a second
later. Every write endpoint in this service had the same shape.

`get_session` commits in its exit code, after the `yield`. A `yield` dependency
in FastAPI defaults to *request* scope, which runs that exit code after the
response has been sent. So the order was: route returns, `201` goes out, then
the row is written.

The visible half is a read-your-writes failure, and in this console it is not
cosmetic — it redirects to the invoice straight after a decision, so it could
render a just-approved invoice as undecided, form and all, and invite a second
decision. The invisible half is worse: a commit that failed at commit time — a
constraint, a dropped connection — left the client holding a `201` for a
decision that was never recorded.

`SessionDep` is `Depends(get_session, scope="function")`: the commit runs when
the route returns and before the response is written, so a failure becomes a
`500` and a success is readable the moment the client has it. Measured after:
**0 of 12**. Two tests hold it:

- `test_session_scope.py` walks every route, recursively through
  sub-dependencies, and fails if any takes a session any other way. Its first
  version passed while checking **zero routes**: FastAPI 0.141 no longer
  flattens included routers into `app.routes`, so a flat scan finds nothing and
  reports no offenders. A second test now asserts the scan found the routes it
  should, so a future change of shape turns red instead of quietly becoming a
  no-op. Both were proven by reverting one route and watching the first fail.
- `TestTheResponseFollowsTheCommit` repeats the original measurement against the
  live stack, with no sleep, six times.

The integration suite had been green throughout, because every test that wrote
something then polled for it. Polling is correct client behaviour for
asynchronous work, and it was hiding a synchronous bug.

---

## D-017 — A person decides

**Date:** 2026-09-10 · **Status:** accepted · **Has known, unpaid costs**

M4 turned `disposition` from a string nothing read into something consequential.
The calls that were genuinely contested:

**You may decide an invoice only if you own every open exception on it.** The
obvious rule, "the owner of the worst exception", is wrong in a specific and
dangerous way. Exceptions sort by severity and then by code, so among blocking
codes the alphabet breaks the tie, and `arithmetic_mismatch` (ap_clerk) sorts
ahead of `bank_details_changed` (treasury). An altered total plus redirected
remittance is the likeliest composite fraud in this domain, and a worst-owner
rule hands it to a clerk who can clear treasury's finding without treasury ever
seeing it. `test_approval.py` guards the premise itself, so the test cannot
quietly stop testing anything if the taxonomy changes.

The consequence is stated rather than hidden: an invoice whose exceptions have
two different owners cannot be approved by one person, because there is no
multi-party approval yet. It stays blocked and says which roles it needs.

**Decisions attach to a `match_result`, not a document.** A decision is about one
body of evidence. When a new version lands, the old approval does not refer to
it, and nothing needs invalidating.

**`auto_approved` and `held` are not terminal.** Only approve and reject are,
enforced by a partial unique index. A terminal auto-approval would make D-016's
documented worst gap — an unreadable payment block auto-approves — permanently
unreachable by a person.

**The console is a back-end for front-end.** The browser never holds a token;
the console exchanges the code server-side and keeps the token in an httpOnly
cookie. It authorises payment and renders vendor-supplied text, and a single XSS
in a public-client console would hand out a bearer token for it. PKCE is written
by hand rather than through a library, so the four things that matter — `state`,
the verifier, server-side exchange, and the public/internal issuer split — are
visible to a reader rather than behind an import.

**The realm was reconciled to the taxonomy, never the reverse.** `receiving` and
`treasury` owned exceptions and were not realm roles. Renaming an `OwnerRole`
would have been the smaller diff and would have rewritten the owner stamped on
every exception already stored.

Found and fixed while verifying, each of which would have failed quietly:

- **The stack came back half-up after a Docker VM restart.** `api`, `worker`
  and `console` had `restart: unless-stopped`; Postgres, Redis, MinIO and
  Keycloak had no policy, exited together with code 255, and stayed down for
  eleven hours while the API reported healthy — correctly, since liveness is not
  readiness, and uselessly. Every long-running service now restarts.
- **The console's healthcheck tested the wrong thing.** It fetched `/queue`,
  which redirects to login, which redirects to Keycloak's *public* URL. `fetch`
  follows redirects, so it was asking whether `localhost:8080` was reachable from
  inside the container, which it never is. The console reported unhealthy while
  serving correctly. It now has a liveness route of its own.
- **Backslashes collapsed in inline edits on this shell.** Several lines of
  `smoke.sh` had acquired raw escape bytes and literal newlines across the last
  few milestones, including a `tr -d '\r'` that had become `tr -d '<newline>'`.
  Scripts are now written from files, and `smoke.sh` contains no control bytes.

**Unpaid costs:**

- **Readiness checks the wrong dependencies.** *(Closed by D-025.)* It checks Postgres and Redis.
  Nothing in the application uses Redis; MinIO (ingest) and Keycloak (every
  authenticated route) are used and not checked, so with only Keycloak down,
  readiness says ready while authenticated calls return 503. The smoke check was
  renamed to say what it actually checks rather than "every dependency". M5 owns
  the fix, which includes deciding whether a shared IdP belongs in readiness at
  all — pulling every instance out of rotation during an IdP blip is often worse
  than serving a clear 503.
- **Worker audit rows have no trace id.** A worker has no request to correlate
  to. M5 propagates a trace through the queue payload.
- **No multi-party approval**, so composite invoices with two owners cannot be
  cleared in M4, and no segregation of duties between whoever resolves an
  exception and whoever approves.
- **The console has no browser-driven test.** `scripts/check_console.py` drives
  it server-side as two roles, including a no-JavaScript submission, but nothing
  runs a real browser against it. Typing a password into Keycloak's form from an
  automated agent was deliberately not done; a person signing in is the
  verification that remains.

---

## D-016 — Payment-detail comparison, and the four ways it can decline to answer

**Date:** 2026-09-10 · **Status:** accepted · **Closes D-013** · **Has a known, unpaid cost**

`BANK_DETAILS_CHANGED` now fires. An invoice that is arithmetically perfect —
right order, right price, goods received — is blocked to treasury on a four-hour
clock when it asks to be paid into an account this vendor has never used.
`make demo-match` shows it against a real PDF.

**Tail-to-tail, not the fingerprint.** D-013's written plan named
`VendorBankAccount.account_fingerprint`, and that plan was not executable: the
fingerprint is an HMAC over the *full* routing and account numbers, while every
one of these layouts prints four digits of nineteen by deliberate design. The
comparison is therefore on `account_number_last4` and `routing_number_last4`.
Recorded plainly because it is a weaker check than D-013 implied, and the
honest name for it is tail-to-tail matching of whatever both sides happen to
carry — the invoice may print an IBAN tail while the column holds a domestic
account tail. For a German IBAN those coincide. In general they are not the same
field, and that gap predates this work.

**A state, not a boolean.** `InvoiceFacts.bank_comparison` is a required
`BankComparison` with no default. The interesting states are the ones that are
neither matched nor changed, and a `bool` collapses all of them into `False` —
which reads as *checked and fine*. That is precisely the shape D-013 was written
against, so replacing one defaulted flag with another would have reproduced the
bug in a new place.

**Both identifiers must read, or nothing is claimed.** A routing-only match is
agreement about which *institution* is being paid while never having seen the
destination account — the most dangerous available false negative. All four
layouts print both, so the strict rule costs nothing on the real corpus.

**A routing-only difference is a change.** The tempting softer rule — treat it
as informational because bank mergers and correspondent routes exist — was
rejected. It converts the flagship control into a fail-open path on an unmeasured
guess about fraud shapes, which is exactly what D-014 forbids. And the tolerance
philosophy does not transfer: "immaterial" there means eleven cents of
reconcilable rounding, whereas there is no immaterial fraction of an account
identifier.

**Retired is distinguished from unknown.** An invoice quoting an account this
vendor has *replaced* is a stale or replayed document; one quoting an account we
have never held is a redirect attempt. Same code, same severity, same desk, same
clock — a different first phone call. It costs one enum member and uses the
effective-dating the seed already keeps for exactly this question.

**Four ways to decline, all recorded.** `NOT_EXTRACTED` (the reading predates
this feature — re-extract), `NOT_READ` (nothing legible enough to accuse anyone),
`NO_VENDOR`, and `NO_HISTORY` each write a distinct sentence into
`skipped_checks`. No new taxonomy code. `NO_HISTORY` is live on the seed today
and is the shape of every genuine first invoice from a new supplier; firing the
highest-severity code on that would be a false positive by construction.

The schema field is `ExtractedField[str] | None = None` rather than a default
not-found instance, and that distinction is the whole reason `NOT_EXTRACTED`
exists. `Extraction.fields` is a JSONB blob written once, so rows persisted
before this feature have no such key. A default instance would make "never
asked" byte-identical to "asked and found nothing" — two different facts with
two different remediations. `_empty_result` therefore passes the not-found
instances *explicitly*: a document with no text layer genuinely was asked.

**Not decision-relevant.** The fields are deliberately absent from
`decision_relevant_fields()`. That gate is a strict minimum and an unreadable
payment line scores 0.0, so every invoice without a printed bank block would
trip `LOW_CONFIDENCE_FIELD`. The signal has its own first-class path through the
exception layer, and `lowest_decision_confidence` is additionally a denormalised
column written once at ingest — recomputing it under a new definition would
leave historical rows disagreeing with current ones forever.

**The read floor is a module constant, not a policy knob.** A tolerance answers
"how much drift is acceptable"; this answers "did we read it at all", which is
identity rather than degree. It is also a property of one parser's scoring tiers,
so stamping it into the versioned `TolerancePolicy` would dress a second guess as
calibration at the exact milestone D-014 says not to.

**The unpaid cost, stated plainly.** An invoice whose payment block this parser
cannot read auto-approves with a recorded skip. An escalation on unreadability
was considered and rejected on a technical ground rather than a philosophical
one: its trigger would be computed from the same label list whose failure
produces it, so an unlisted label — `Beneficiary account`, `Empfängerkonto` —
yields "no payment block present" and therefore silence. That is a correlated
detector on the shortest clock in the system. The real fix is parser coverage,
and eventually a full-detail comparison at payment-file generation where the
complete account number exists.

One consequence worth knowing: because the clean path now genuinely compares,
`smoke.sh`'s "no check was skipped" and the integration suite's
`skipped_checks == []` became stronger assertions. A parser regression on the
continental layout presents as a smoke failure about skipped checks, which is one
indirection away from obvious.

Two things fixed in passing. The API was returning the whole extraction blob, so
the payment digits would have been served to any caller; `redacted_fields` drops
them — dropped, not masked, because four digits is a short candidate list. And
`GET /v1/documents/{id}` is the first consumer of that projection.

---

## D-015b — The golden fixture had been quoting a bank account that never existed

**Date:** 2026-09-10 · **Status:** accepted

`build_spec()` printed account `8291` — Northgate's current account — paired with
routing `0248`, the routing code Northgate *retired* forty days before
`SEED_EPOCH`. Each value was individually plausible; the pair existed on no row
in the database.

It had been wrong since M1 and was invisible for exactly as long as nothing read
it. The moment payment-detail comparison went live it turned the golden "clean"
invoice into the highest-severity exception in the system and collapsed every
demo scenario to blocked.

The fixture was corrected rather than the seed. The seed's bank rows are keyed on
a fingerprint that includes `effective_from`, and inserts are
`on_conflict_do_nothing`, so editing a digit and re-seeding a warm database
*inserts* rather than replaces — and for the open row that produces two rows both
claiming to be current, which is the one ambiguity the comparison has to tolerate.

`tests/unit/test_seed_coupling.py` now asserts the coupling directly: the
fixture's payment details must equal the last four digits of the vendor's
currently-effective row, its cited purchase order must exist, and every line it
bills must have a matching SKU and agreed price. No database needed — both sides
are Python literals — so it runs on the `make verify` side of the split.

This is the same lesson as D-015, one layer out. A value that nothing reads is
not validated by being written down, and the comment that documented the coupling
was not a test.

---

## D-015 — Using the extracted values is what proved they were wrong

**Date:** 2026-09-10 · **Status:** accepted

The extraction tier shipped in M1 with tests passing. Every one of those tests
asserted that a field was read; none asserted it was read *correctly against
something that would notice*. The first invoice put through the matcher came
back with three defects, all of which had been there since M1:

- **The purchase-order number lost its prefix.** `PO-44718` parsed as the label
  `PO` followed by the value `-44718`, the token pattern dropped the leading
  hyphen, and the field came out `44718` — a number matching no order on file.
  A label is only a label when something separates it from the value, so the
  label pattern now requires that separator.
- **The VAT rate was being read as the VAT amount.** The line prints as
  `zzgl. MwSt. 19,00 %` with the amount `58,29` on the following line, so the
  rate is nearer the label and scores higher. The invoice came out carrying
  19.00 of tax instead of 58.29. A percentage is never a money amount, whatever
  line it sits on.
- **There were no line items at all.** `line_items=()` with a comment deferring
  it to M3. A three-way match with no lines has no price to compare and no
  quantity to compare, which is the entire check.

The first two were caught by the arithmetic corroboration already in the
extractor — subtotal plus tax did not reconcile, so confidence dropped and the
invoice blocked rather than paying. The mechanism worked. It just could not say
*which* number was wrong, and nothing downstream was consuming the value closely
enough to find out.

The general lesson is not about extraction. A field that is stored and never
read is not verified by being stored, and a test that asserts a value exists
verifies less than it appears to. These bugs survived a milestone because the
only consumer was a JSONB column.

Line-item extraction reads a record's numbers from the *end*. `extract_text`
flattens a table to one line per cell, so a wrapped description like
"…boxed / 100" puts a stray number in the middle of the record; taking the first
three numbers reads a quantity of 100 and a unit price of 12. The last three are
always quantity, unit price and line total, because they are the last three
columns. Records are separated by position numbers, identified as the
subsequence that counts 1, 2, 3… — the stray `100` looks exactly like a position
otherwise, and was splitting every record in half.

Anything not fitting that shape yields no line items rather than guessed ones,
and the matcher records "no line items" as a skipped check. That is the point:
before this, an invoice with no lines matched clean, because there was nothing
to disagree with. An invoice that was never compared must not read the same as
one that was compared and passed.

---

## D-014 — The auto-approval gate is provisional, and says so

**Date:** 2026-09-10 · **Status:** CLOSED by D-027 on 2026-09-12

> Kept rather than rewritten. It named the cost precisely and set the terms the
> derivation had to meet, and both are worth reading beside the answer. The
> number it defends, 0.50, turned out not merely to be unvalidated but to be
> wrong in the expensive direction: it admitted a bucket of invoices that were
> measurably always misread.

`minimum_field_confidence` is 0.50. It is the weakest number in the system and
the one most likely to be wrong.

A gate is a probability threshold: below *this likelihood of being right*, do
not act without a human. The deterministic tier does not produce probabilities.
It produces an ordinal signal — 0.88 means "found immediately after its label",
0.62 means "found on the following line", 0.45 is a hardcoded admission that
finding an unlabelled vendor name near the top of a page is guesswork, and 0.0
means "not found". None of those numbers has been measured against how often
that reading is actually correct.

So any threshold is a guess. The honest response is to pick the only boundary
those numbers genuinely support — between "read something" and "read nothing" —
and to say plainly that it is provisional. M6 builds the calibration curve, and
this number has to be re-derived from it rather than tuned until the demo looks
good. Tuning it against a fixture is how a control becomes a decoration.

Two things follow from taking that seriously:

**Vendor identity is corroborated, not scored.** The extractor rates the vendor
name 0.45 by construction. But once that string has matched a row in the vendor
master, the identity no longer rests on the heuristic — an independent source
agreed. So the matcher drops `vendor_name` from the gate *when resolution
succeeded*, which is the same argument the extractor already makes when subtotal
plus tax reconciles to the total. A name that fails to resolve is not discounted
at all: it has already raised `VENDOR_UNKNOWN`, which blocks.

**The stored score is left alone.** `Extraction.lowest_decision_confidence`
still records the raw reading. The corroborated value is a different question
asked at decision time, and collapsing the two would destroy the number M6 needs
to calibrate against.

The unpaid cost is plain: until M6, this system's auto-approval boundary rests
on a number nobody has validated. Every other threshold here can be argued from
the domain. This one cannot yet.

---

## D-013 — Bank-detail changes are detected but not yet detectable

**Date:** 2026-09-10 · **Status:** CLOSED by D-016 on 2026-09-10

> Kept rather than deleted. The gap it names is real history, and having
> named it before closing it is worth more than a tidy log. Note that its
> proposed mechanism — comparing the stored HMAC fingerprint — turned out not
> to be executable: the fingerprint covers the full account number and an
> invoice prints four digits of it. D-016 explains what replaced it.

`BANK_DETAILS_CHANGED` is in the taxonomy, routes to treasury with the shortest
clock in the system, and is covered by the match table. `MatchingService` sets
its flag to `False` unconditionally, so in the running pipeline it can never
fire.

The reason is a gap in the middle. `VendorBankAccount` keeps payment details as
history specifically so "changed since we last paid them" is answerable, and the
invoice fixtures render a masked IBAN on the page. But `ExtractedInvoice` has no
field for payment details and the extractor does not read them, so there is
nothing to compare the history against.

This is recorded rather than quietly worked around because it is the flagship
control. The architecture document calls a vendor bank change the
highest-severity exception the system knows about, and it is the shape almost
every real accounts-payable fraud takes. A reader who sees the code in the
taxonomy is entitled to assume it fires.

Closing it needs three things: a payment-details field on the extraction schema,
a parser for the masked IBAN and sort-code shapes the layouts render, and a
comparison against the current `VendorBankAccount` fingerprint. None of it is
hard. It was left out of M3 rather than half-done inside it, because a detector
wired to a field nobody populates is worse than no detector — it reads as
covered.

---

## D-012 — The queue tests own their database, and `claim` sorts what it returns

**Date:** 2026-09-09 · **Status:** accepted

Two findings, one cause: the queue tests had been sharing a database with a
live worker.

`make up` leaves a worker polling the application database once a second. It is
a real consumer running the same claim query, so it competes for jobs a test
just enqueued and then fails them, because their payloads name documents that do
not exist. Every assertion about who claimed what, how many attempts a job has,
and what reached the dead-letter table was only holding because the worker
happened to be down. A suite that passes because another process is broken is
not passing.

So `tests/integration/test_queue.py` provisions `threshold_queue_tests`, migrates
it with Alembic and runs there. Alembic rather than `Base.metadata.create_all()`:
the partial indexes are hand-written in `0003_job_queue` because SQLAlchemy
cannot express their predicates, so a schema built from the models would quietly
omit `uq_job_dedupe_key` — the exact constraint several of those tests exist to
verify. The API tests keep using the application database, because they *want* a
live worker: that is the thing they are testing.

Running on a fresh database immediately exposed a real bug the shared one had
been hiding. `claim()` selects with `ORDER BY run_after, created_at` inside the
subquery, which decides *which* rows are taken — but `UPDATE … RETURNING` makes
no promise about the order rows come back in, and Postgres returns them roughly
in heap order. On a table that has seen deletes and is reusing free space, heap
order stops matching insertion order and a batch comes back shuffled. The queue
was FIFO in the part nobody observes and not FIFO in the part a worker actually
sees. `claim()` now re-sorts its result.

Neither of these is exotic. Both are the ordinary consequence of testing against
a database that something else is also using, which is worth the cost of the
extra database on its own.

---

## D-011 — A failed claim retries in-process; only a crash needs Docker

**Date:** 2026-09-09 · **Status:** accepted

Found by running `make migrate-roundtrip` against a live stack. The downgrade
dropped the `job` table underneath a polling worker, the claim raised
`UndefinedTableError`, the exception escaped `run_forever`, and the process
exited. Compose leaves `restart` at its default, so the container stayed down.
The API went on accepting documents and queueing jobs that nobody would ever
run, and nothing anywhere said so.

The schema vanishing is not the interesting part — that was self-inflicted. The
same path is a Postgres restart, an RDS failover, a connection reset, or a
migration running during a deploy, all of which are routine, all of which are
brief, and any of which would have permanently drained the pool.

So the claim loop now catches, logs, and backs off exponentially from the poll
interval to a thirty-second cap, resetting on the first success. This is safe in
a way that retrying is usually not: a claim that raised has claimed nothing, so
there is no partial work to reconcile and no lease to lose. `_execute` already
settled its failures against the job and `_reap_loop` already guarded itself —
the claim path was simply the one that never got the treatment.

`restart: unless-stopped` on `api` and `worker` is the backstop for what
in-process handling cannot cover, which is a crash. It is deliberately not the
mechanism: a pool that needs Docker to notice a ten-second blip has already
dropped whatever it was holding, and on ECS or Kubernetes the supervisor's
reaction time is worse, not better. The one-shot `migrate`, `seed` and
`minio-init` services keep `restart: "no"` — a job that reruns itself is not a
job.

Two things this deliberately does not do. It does not distinguish a transient
database error from a permanent one, because at the claim site that distinction
cannot be made honestly — `UndefinedTableError` is permanent during a downgrade
and transient during a deploy, and the same exception carries both. And it does
not give up after N consecutive failures, so a worker pointed at a database that
will never come back retries until someone stops it. Both are the right trade
while the failure is loud in the logs; neither survives contact with a system
nobody is watching, which is what M5's alert rules are for.

Regression test: `TestPoolSurvivesADatabaseOutage` in
`tests/integration/test_queue.py`. It wraps the real queue so the first three
claims raise, then asserts the worker was still claiming afterwards and the job
ran. Reverting the guard makes it fail.

---

## D-010 — A dead worker's attempt is not counted against the job

**Date:** 2026-09-09 · **Status:** accepted

`reap_expired_leases()` returns a job to `pending` and clears the claim, but
deliberately does not increment `attempts`. The claim already incremented it;
counting it again would charge the job twice for one try.

The reason is whose fault the failure was. A job that fails three times because
the document is unreadable has used its budget legitimately. A job whose worker
was `SIGKILL`ed by an OOM killer has not failed at all — the infrastructure
did — and halving its remaining retries punishes the work for something it had
no part in. In a pool that is being rolled or rescheduled, that difference is
the difference between a deploy that is invisible and a deploy that dead-letters
a slice of the queue.

`scripts/demo_crash_recovery.sh` asserts this directly: it prints `attempts`
before and after the reap and calls out that it is still 1.

The cost is that a job which reliably kills its worker — a payload that
segfaults a parser, say — is retried forever rather than dead-lettering. That is
the right trade at this size, because such a job also takes the worker down and
so becomes visible immediately, but it is a real hole and a poison-pill detector
belongs here before this runs unattended.

---

## D-009 — Deduplication is a partial unique index, not a lookup

**Date:** 2026-09-09 · **Status:** accepted · **Has a known, unpaid cost**

`enqueue(dedupe_key=…)` does not check for an existing job and then insert.
Between the check and the insert, another worker can insert — the classic
read-modify-write race, which under concurrency produces exactly the duplicate
the key exists to prevent. Instead the uniqueness is a database constraint and
the insert absorbs the collision:

```python
base = insert(Job).values(**values)
if dedupe_key is not None:
    base = base.on_conflict_do_nothing(
        index_elements=[Job.dedupe_key],
        index_where=text("dedupe_key IS NOT NULL AND state IN ('pending', 'claimed')"),
    )
```

The index is partial, and that is the whole design: a `dedupe_key` is unique
only among jobs that are still `pending` or `claimed`. Once a job succeeds or
dies, the key is free again, so "do not run this twice concurrently" does not
silently become "never run this again", which would make a retry after a failure
impossible.

Two things here were bugs before they were decisions, and both are the kind that
only appear under load:

- `index_where` is not optional. Without it Postgres cannot match the partial
  index and raises `InvalidColumnReferenceError: there is no unique or exclusion
  constraint matching the ON CONFLICT specification`. Every deduplicated enqueue
  would have failed — and the unit tests, which exercise the in-memory queue,
  would not have noticed.
- `on_conflict_do_nothing()` must be applied **before** `.returning()`. The
  other order compiles and silently drops the conflict clause.

The unpaid cost is the flip side of freeing the key on success: re-submitting
the same document after its extraction finished enqueues a second, redundant
extraction. The queue is right to allow it; the caller has to decide. So
`IngestService` checks for an existing extraction and skips the enqueue rather
than the queue pretending the work is still in flight.

---

## D-008 — The enqueue commits with the state change, or not at all

**Date:** 2026-09-09 · **Status:** accepted

`Queue.enqueue()` takes the caller's `AsyncSession` rather than opening its own.
Ingestion therefore writes the `document` row and the `job` row in one
transaction, and either both land or neither does.

This is the transactional outbox, and it is the reason there is no broker here.
With SQS or RabbitMQ, "insert the row" and "publish the message" are two systems
and there is no commit that spans them: publish first and a rollback leaves a
message about a document that does not exist; publish second and a crash between
them leaves a document nobody will ever process. Every mitigation for that —
outbox tables, two-phase commit, reconciliation sweeps — is machinery to buy back
what one database gives away for free.

The cost is stated plainly in the architecture doc: this does not fan out, it
does not replay a topic, and the polling loop becomes the bottleneck somewhere
above a few hundred jobs per second. Accounts payable is hundreds of invoices
per **day**. A `Queue` protocol marks the seam, and `PostgresJobQueue` is its
only implementation — there is no SQS adapter here, and moving to one would not
be free, because the single commit this decision buys is exactly the thing a
broker cannot give back. The protocol makes that cost visible at the type level
rather than leaving it to be discovered.

One consequence worth knowing before it confuses someone: jobs enqueued inside a
single transaction share one `now()`, because Postgres freezes
`CURRENT_TIMESTAMP` per transaction. Their `run_after` values are identical and
their relative claim order is therefore undefined. Nothing depends on it, but a
test that assumes FIFO within a batch will flake.

---

## D-007 — Claiming a job is a state change, never a delete

**Date:** 2026-09-09 · **Status:** accepted

A worker claims a job by updating it — `state = 'claimed'`, `claimed_by`,
`claimed_at`, `lease_expires_at`, `attempts + 1` — inside
`SELECT … FOR UPDATE SKIP LOCKED`. It never deletes the row and never holds the
job in process memory.

`SKIP LOCKED` is what makes the pool scale without coordination: a worker whose
candidate row is already locked steps over it rather than queueing behind it, so
N workers do not serialise on the head of the queue. The integration suite runs
four workers against forty jobs and asserts every job was claimed exactly once.

The delete-on-claim alternative is the more obvious design and it is why so many
homegrown queues lose work. Once the row is gone, the only record that the job
exists is in the memory of the process holding it, and a `SIGKILL` — an OOM
kill, a node preemption, a `docker compose kill` — takes that record with it.
There is nothing left to recover from.

Because the row survives, recovery is just noticing that a lease expired. The
reaper (running under advisory lock `0x74855170`, so exactly one worker sweeps
regardless of pool size) returns any job whose `lease_expires_at` is in the past
to `pending`, with its payload and attempt history untouched.

The cost is that the queue is a table that grows and needs a retention policy,
and that every claim is a write, so the queue is not free at high throughput.
Both are acceptable at this volume; neither is a correctness problem.

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
