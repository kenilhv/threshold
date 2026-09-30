---
version: 1
slug: "console-app-queue-page-tsx"
primary_target: "console/app/queue/page.tsx"
related_targets: ["console/app/invoice/[id]/page.tsx","console/app/ledger/page.tsx"]
---

# Operator console: queue, invoice, books

Scope: `/queue` (primary), `/invoice/[id]`, `/ledger`. Mode: Operate.

Audience and job: an AP operator (buyer, clerk, receiving, treasury) working
blocked invoices for hours at a desk; decide each one quickly and defensibly.
Constraints: works with JavaScript off, no restricted data in HTML, voice and
four URLs unchanged (PRODUCT.md). Chosen unattended: the direction page went
unanswered, so the roll's assignment stands.

## Direction contract

THESIS: The queue is the morning's exception run, printed on green-bar
continuous stationery. It refuses the SaaS admin dashboard: no sidebar, no KPI
tiles, no cards, no status pills.

OWN-WORLD: Cool paper white with pale green bands on alternate rows; form
headings and rules preprinted in a condensed grotesque in green ink; every
figure line-printed in a condensed mono in black; a second, red ribbon used only
for what stopped (blocked, over, overdue). Square corners, hairline and double
rules, no shadows. Totals carry the accountant's double rule.

STORY: The operator sees what is waiting, which lines are theirs, why each
stopped in plain words with the numbers, and records a decision on a printed
form section without decoding anything.

FIRST VIEWPORT: Report heading "Waiting for a person" left, run line in mono
right (reader, count waiting, count yours, filter). Below, banded rows at printer
pitch: vendor and amount, why it stopped, owner, due, action. Hovering or tabbing
lays a reading ruler under the current row (signature interaction, 150 ms).

FORM: Green-bar Report, position 4 of 7, seed 7f6d8970. Raises kept: printer-line
module; no bare numbers; payment stations in order; no containers; one state
vocabulary for "not yours"; one active edge.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
