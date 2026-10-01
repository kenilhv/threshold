# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Accounts-payable staff at a company that buys goods against purchase orders,
working the review queue as their daily job. Six realm roles, each owning a kind
of problem rather than a kind of invoice:

- **AP clerk** — reading problems: the document could not be read confidently,
  a field is missing, the arithmetic on the invoice does not add up, a
  duplicate is suspected.
- **Buyer** — commercial problems: billed above the agreed price, billed for
  more than was ordered, a line that is not on the order.
- **Receiving** — billed for goods nobody has recorded as arriving.
- **Treasury** — the invoice asks to be paid into an account this vendor has
  never used before.
- **Controller** and **auditor** — oversight; the auditor reads everything and
  decides nothing.

The scene: someone with a queue of blocked invoices, deciding each one quickly
and defensibly, several hours a day, most often at a desk on a laptop or a
second monitor. Design is judged against that person (confirmed 2026-09-30).
Hiring managers and reviewers are the people who will actually open it; they
are persuaded by a tool that looks used in anger, not by one designed to
impress them.

## Product Purpose

Take an invoice from arrival to payment without a person touching it, unless
the system cannot prove it should. Every clean invoice is matched against its
purchase order and the goods received, approved by the system, paid through a
payment rail, and written into a double-entry ledger. Every invoice the rules
will not clear waits for exactly the person who owns that particular problem,
with the evidence laid out and the reason stated.

Success for the console: a reviewer can see why an invoice stopped, whose it is,
and what the evidence says, and record a defensible decision, without leaving
the page or decoding anything.

## Positioning

Threshold does not score invoices or ask a model to approve them. It runs a
three-way match whose every exception has a code, a severity, an owner and a
deadline, and it routes by ownership: you may decide an invoice only if you own
every open exception on it. The database, not the application, refuses what
must never happen: an edited audit log, an unbalanced ledger, a payment
redirected after approval, a second live payment for one invoice.

## Operating Context

- The queue lists only invoices that raised an exception; clean ones never
  appear because the system already decided them.
- An invoice's evidence is the purchase order (what was ordered, at what agreed
  price), the goods receipt (what arrived), and the invoice itself (what is
  billed). A three-way match is those three compared line by line.
- Each exception carries a code from a closed taxonomy, a severity (block,
  review, info), an owning role, and an SLA deadline.
- Decisions are approve, reject or hold, and every human decision requires a
  written reason.
- Money is in minor units with an explicit currency; invoices arrive in EUR,
  GBP and USD in the demo data.
- A payment moves pending → submitting → submitted → settled, or fails; the
  ledger ("Books") must always balance to zero.

## Capabilities and Constraints

- Next.js console, server-rendered, back-end for front-end: no page script can
  read the access token (httpOnly session cookie).
- **Must work with JavaScript disabled.** Deciding an invoice is a plain HTML
  form; `scripts/check_console.py` tests it end to end (confirmed binding).
- **No restricted data in HTML.** Bank digits and tax IDs never reach the
  browser; also tested (confirmed binding).
- **Four pages, stable URLs:** landing `/`, queue `/queue`, invoice
  `/invoice/[id]`, books `/ledger` (confirmed binding).
- Data comes from the FastAPI service; the console renders what the API
  returns and computes nothing about money itself.
- Accessibility floor: WCAG 2.2 AA contrast and keyboard operability; status
  is never carried by colour alone.

## Brand Commitments

- Name: Threshold.
- **Voice is binding** (confirmed): dry, exact, explanatory sentences that say
  why, e.g. "Invoices that raised nothing are not listed: the system already
  decided those, and a queue containing work nobody has to do teaches people to
  skim it." No marketing tone, no exclamation, no reassurance for its own sake.
- No logo or visual identity exists yet.

## Evidence on Hand

- A running local stack with a reproducible demo state: `make demo-match` and
  `make demo-queue` produce seven invoices across three vendors (Northgate
  Supply, Beckman Fasteners, Halcyon Facilities) in EUR and GBP, covering
  auto-approval, review and block. All vendors, orders and amounts are
  synthetic.
- Measured numbers in `docs/measurements/`, an extraction evaluation over 400
  synthetic documents in `evals/`, a threat model, and 40 decision records.
- **Absent, and must not be fabricated:** real customers, testimonials, logos
  of companies using it, production deployment, uptime or volume figures,
  pricing, certifications.

## Product Principles

1. **Say why it stopped, and whose it is.** Every blocked invoice states the
   rule that fired, by how much, and the one role that owns it.
2. **Show the evidence, don't summarise it away.** Ordered, received and billed
   sit side by side; the reviewer should never have to trust a verdict they
   cannot check.
3. **Density is respect for the operator's time.** A reviewer scans twenty
   rows; whitespace that costs them rows is decoration.
4. **The default is to ask a person.** When a control could not run, the
   system says so and does not pretend the invoice is clean.
5. **Nothing looks staged.** It should read as a tool that runs a finance
   team's week, not a demo of one.

## Accessibility & Inclusion

WCAG 2.2 AA: text contrast, visible focus, full keyboard operation, and status
never conveyed by colour alone. The decision form works without JavaScript.
