---
name: Threshold
description: Invoices the system would not clear on its own, printed as an exception run on green-bar stationery.
colors:
  paper: "#f4f7f2"
  band: "#e3eedf"
  ruled: "#d3e4cd"
  select: "#c4ddbd"
  rule: "#a8c5a2"
  form: "#3b6843"
  ink: "#1a1f1b"
  ink-soft: "#465049"
  red: "#ae2219"
typography:
  display:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "clamp(2.5rem, 4.2vw, 3.75rem)"
    fontWeight: 800
    lineHeight: 1.02
    letterSpacing: "-0.02em"
    fontVariation: "'wdth' 75"
  headline:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "2rem"
    fontWeight: 800
    lineHeight: "2.75rem"
    letterSpacing: "0"
    fontVariation: "'wdth' 75"
  title:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 750
    lineHeight: "2.0625rem"
    fontVariation: "'wdth' 75"
  body:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: "1.375rem"
    fontVariation: "'wdth' 92"
  label:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 700
    lineHeight: "1.375rem"
    letterSpacing: "0.08em"
    fontVariation: "'wdth' 75"
  label-column:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 700
    letterSpacing: "0.08em"
    fontVariation: "'wdth' 75"
  label-button:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 700
    letterSpacing: "0.08em"
    fontVariation: "'wdth' 75"
  brand:
    fontFamily: "Archivo, 'Arial Narrow', system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 800
    letterSpacing: "0.14em"
    fontVariation: "'wdth' 75"
  print:
    fontFamily: "'Martian Mono', ui-monospace, 'Cascadia Mono', Consolas, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
    letterSpacing: "0"
    fontFeature: "'tnum' 1, 'zero' 1"
    fontVariation: "'wdth' 87.5"
  status:
    fontFamily: "'Martian Mono', ui-monospace, 'Cascadia Mono', Consolas, monospace"
    fontSize: "0.75rem"
    fontWeight: 600
    letterSpacing: "0.04em"
    fontVariation: "'wdth' 87.5"
  figure-total:
    fontFamily: "'Martian Mono', ui-monospace, 'Cascadia Mono', Consolas, monospace"
    fontSize: "1.75rem"
    fontWeight: 500
    fontFeature: "'tnum' 1, 'zero' 1"
    fontVariation: "'wdth' 87.5"
rounded:
  none: "0"
spacing:
  line-quarter: "0.34375rem"
  line-half: "0.6875rem"
  line: "1.375rem"
  line-double: "2.75rem"
  cell-inline: "0.75rem"
  page-gutter: "1.5rem"
  page-gutter-narrow: "1rem"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    typography: "{typography.label-button}"
    rounded: "{rounded.none}"
    padding: "0 1.25rem"
    height: "2.75rem"
  button-primary-hover:
    backgroundColor: "{colors.form}"
    textColor: "{colors.paper}"
  button-secondary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.label-button}"
    rounded: "{rounded.none}"
    padding: "0 1.25rem"
    height: "2.75rem"
  button-secondary-hover:
    backgroundColor: "{colors.ruled}"
    textColor: "{colors.ink}"
  button-quiet:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "0 0.6rem"
    height: "1.375rem"
  nav-link:
    textColor: "{colors.form}"
    typography: "{typography.label}"
  nav-link-active:
    textColor: "{colors.ink}"
  table-row-band:
    backgroundColor: "{colors.band}"
    padding: "0.6875rem 0.75rem"
  table-row-ruler:
    backgroundColor: "{colors.ruled}"
  column-title:
    textColor: "{colors.form}"
    typography: "{typography.label-column}"
    padding: "0 0.75rem 0.34375rem"
  textarea-ruled:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.none}"
    padding: "0 0.25rem"
  station-current:
    backgroundColor: "{colors.band}"
    textColor: "{colors.ink}"
    padding: "0.6875rem 0.75rem"
---

# Design System: Threshold

## Overview

**Creative North Star: "The Green-bar Report"**

Every screen is the morning's exception run, printed on continuous-form line-printer stationery. The metaphor is kept only where it does a job. The bands on alternate rows are there for the reason they were always there: they hold the eye on one row across a wide table. The two inks are provenance. Green is the form, preprinted before any invoice existed: headings, column titles, instructions. Black is what the system printed onto it: vendors, figures, reasons. A second, red ribbon prints only what stopped. Anything else from the paper (tractor holes, tear-off stubs) did no work here and is not here.

The page is dense on purpose and flat on purpose. There are no containers: structure comes from rules printed on the paper (hairline, dashed perforation, accountant's double) and from the printer-line module every vertical measure is cut from. Headings are condensed and green; figures are condensed mono, tabular, right-aligned. It should read as a tool that runs a finance team's week, not a demo of one.

Motion is one moment: hovering or tabbing through a table lays a reading ruler under the row you are on, the way these printouts were read with a ruler under the line. Dark mode is the same report under a dim lamp: the paper goes near-black green, the form ink lightens, the ribbon stays red.

**Key Characteristics:**
- Two inks by provenance: green for the form, black for the print, red only for stops.
- Pale green bands on alternate table rows; a darker ruled band plus a 2px ink underline for the row being read.
- Printer-line vertical module (1.375rem) and its halves and quarters.
- Square corners, rules instead of boxes, no drop shadows.
- Condensed grotesque for the form's voice, condensed mono with tabular, slashed-zero numerals for everything printed.

## Colors

A cool, near-white paper tinted green, three tints of the same green for bands and rules, one mid green ink, a green-black ink, and a single red.

### Primary
- **Form Green** (#3b6843): the preprinted ink. Page and section headings, column titles, form labels, navigation, instruction text (the lede, hints, the verdict's explanation), heading rules, the double rule under the masthead and above the decision form, the dashed perforation between report sections. Dark scheme: #8fc795.

### Secondary
- **Ribbon Red** (#ae2219): the second ribbon. Only what stopped: a BLOCKED status, a billed figure over the agreed one and its "over" mark, a failed payment station, a conflict the server refused, the textarea caret. Dark scheme: #ff8e80.

### Neutral
- **Paper** (#f4f7f2): the page and every field background. Dark scheme: #111612.
- **Band** (#e3eedf): alternate table rows, the current payment station, inline code. Dark scheme: #182019.
- **Ruled Band** (#d3e4cd): the reading ruler under the hovered or focused row; hover fill for secondary buttons. Dark scheme: #243226.
- **Selection Green** (#c4ddbd): text selection only. Dark scheme: #2f4a34.
- **Hairline Green** (#a8c5a2): hairline dividers between findings and sections, the writing lines in the textarea, station separators, nav hover underline, the scrollbar thumb. Dark scheme: #37523c.
- **Print Black** (#1a1f1b): everything the system printed: vendor names, figures, reasons, body copy; the primary button fill; the focus outline; totals rules. Dark scheme: #dfe8dc.
- **Faded Print** (#465049): secondary print: units, timestamps, counts, "not yours to decide", the agent's quoted block, notes. Dark scheme: #aab8a8.

### Named Rules
**The Two Inks Rule.** Green is what was on the form before the invoice arrived; black is what the system printed onto it. If a string came from the data, it is black. If it explains the page, it is green.

**The Red Ribbon Rule.** Red prints only what stopped (blocked, over, failed, refused), and always beside words that say the same thing. A refusal that is the system working ("not yours", "already decided") is printed in the form's ink, never red.

## Typography

**Display Font:** Archivo (with Arial Narrow, system-ui), variable width axis, set condensed.
**Body Font:** Archivo at 92% width.
**Label/Mono Font:** Martian Mono (with ui-monospace, Cascadia Mono, Consolas), variable width axis, set at 87.5%.

**Character:** Two faces because the page has two voices. Archivo condensed is the preprinted business form; Martian Mono is the line printer, with tabular, slashed-zero numerals so a column of amounts lines up by digit. Both are self-hosted at build time.

### Hierarchy
- **Display** (800, clamp(2.5rem, 4.2vw, 3.75rem), 1.02): the front door's headline only.
- **Headline** (800, 2rem, two printer lines): the report title, left of the run line. Drops to 1.625rem on one and a half lines under 760px.
- **Title** (750, 1.25rem, one and a half lines): section headings within a report ("Why it stopped", "Line by line", "Your decision").
- **Body** (400, 0.9375rem, one printer line): prose, table cells, vendor names. Instruction and prose blocks cap at 68ch (60ch for hints, 72ch for verdicts and refusals).
- **Label** (700, 0.8125rem, 0.08em, uppercase): navigation, form labels, report-section rows. Column titles use the same voice at 0.75rem; buttons at 0.875rem.
- **Print** (mono 400, 0.8125rem, tabular slashed-zero): every figure, date, code, username and the run line. Units follow amounts at 0.75em in Faded Print.
- **Status** (mono 600, 0.75rem, 0.04em, uppercase): BLOCKED, REVIEW, DECIDED; the end-of-run line.
- **Figure total** (mono 500, 1.75rem): the one invoice total, carrying the accountant's double rule.

### Named Rules
**The Column Title Rule.** Column titles are the form's, even over a column of figures: condensed, green, uppercase, right-aligned over numbers but never set in the mono.

**The No Bare Number Rule.** A figure is never printed without its column title or unit. When tables stack on narrow screens, each cell prints its column title beside it.

## Layout

A single centred column, max 78rem, with 1.5rem gutters (1rem under 760px). The masthead is sticky, two printer lines tall, closed by a green double rule. Each report opens with a report head: title left, run line right (mono, Faded Print with counts in Print Black), one green hairline under both. No label sits above the title.

Every vertical measure is a whole printer line (1.375rem), or a half or quarter of one, so the page keeps its pitch however much text an invoice brings. Table cells are half a line top and bottom and 0.75rem inline; section gaps are two lines; the page ends with four lines of foot.

Tables are full width and are the primary layout. Under 760px, tables marked to stack become one printed record per row, each value beside its green column title in a 7.5rem label column. The decision form is two columns (17rem choices, remainder for the reason) and collapses to one. The front door is a 5:6 split: headline, lede and sign-in left, specimen right. At 1200px and up, a deadline and its timestamp share one line.

## Elevation & Depth

Flat. There are no drop shadows and nothing floats. Depth is replaced by tone and rule: bands on alternate rows, a darker ruled band under the row being read, hairlines between records, dashed perforations between report sections, double rules to close a section or a total. The only `box-shadow` in the system is an inset 2px bottom line, which is a printed underline, not elevation.

### Named Rules
**The Rules Not Boxes Rule.** Group with a printed rule, never with a container. Hairline (1px) separates records, dashed (1px) marks a new page of the run, double (3px) opens a major section or closes a total.

**The Reading Ruler Rule.** The one motion: on hover or focus-within, a table row takes the Ruled Band fill and a 2px Print Black underline (`inset 0 -2px 0`), over 150ms on `cubic-bezier(0.16, 1, 0.3, 1)`. It is removed under reduced motion. Nothing else animates for effect.

## Shapes

Square corners everywhere (0 radius), including buttons and the decision boxes. Borders are rules: 1px hairlines, 1px dashed perforations, 3px double rules, 2px solid for button outlines and the focus edge. The only drawn shape beyond rules is the square selection box (1.125rem, 1.5px green border) whose fill is a solid ink square.

## Components

### Buttons
Printed, square, uppercase; a rubber-stamp block rather than a pill.
- **Shape:** square (0), 2px Print Black border, exactly two printer lines tall.
- **Primary:** Print Black fill, Paper text, 0 1.25rem padding. Used for the single commitment on a page (record decision, sign in).
- **Hover / Focus:** primary turns Form Green, border included; secondary takes the Ruled Band. Focus is the global 2px ink outline at 2px offset. Pressed nudges 1px down. Disabled shows a progress cursor at 70% opacity.
- **Secondary:** Paper fill, Print Black text and border.
- **Quiet:** one printer line tall, 0.75rem, 1px border; used for sign out in the masthead.

### Cards / Containers
None. See the Rules Not Boxes Rule.

### Inputs / Fields
- **Style:** the reason field is a writing area, not a box: a green top rule and ruled Hairline Green lines at the printer pitch, so text sits on the lines. Square, no side or bottom border, minimum five lines, vertical resize. Placeholder is italic Faded Print; caret is Ribbon Red.
- **Focus:** the global ink outline at 4px offset.
- **Choices:** decisions are a column of square boxes, each with a label in 600 weight and a one-line consequence in Faded Print below; none is pre-ticked. The ticked box fills with an ink square and its border turns ink.

### Navigation
- Brand in condensed 800, 0.14em tracking, Form Green. Links are labels in Form Green with a 2px transparent underline that turns Hairline Green on hover. The current page is Print Black with a Print Black underline. The reader's name and role sit right in Faded Print with the username in mono. Under 760px the reader line drops to its own row.

### Report Table (signature)
Green uppercase column titles over a green hairline; grouped titles ("Quantity" over ordered, received, billed) over a hairline of their own. Rows band on alternate lines and take the reading ruler. Report sections inside a table are uppercase green rows with a mono count; a new section after the first is preceded by a dashed perforation. Totals close with a black rule above and an ink double rule below. The run ends with a centred mono "END OF RUN" line.

### Verdict and Refusal
The verdict states the status in mono once, beside a green explanation, over a hairline. A refusal (not yours, already decided) is bounded by a green rule above and a hairline below, status in Faded Print. A conflict the server refused uses a 2px red rule above and a red "Not recorded."

### Payment Stations
The payment's stages printed in order across a row, separated by hairlines. The current station takes the Band fill, Print Black text at 600 and a 2px ink underline; a failed current station turns the text and underline red.

### Agent Escalation
Deliberately the quietest block, fenced by dashed green perforations above and below, in Faded Print, with the model's words as an italic indented quote. Set apart, never emphasised: it is what a model said after reading a document the supplier wrote.

## Do's and Don'ts

### Do:
- **Do** cut every vertical measure from the printer line (1.375rem), its half or its quarter.
- **Do** print every figure in the mono with tabular, slashed-zero numerals, right-aligned, with its unit in Faded Print.
- **Do** put the accountant's double rule (3px double, Print Black) under a final figure, and only there among figures.
- **Do** pair every red element with words that say the same thing; status is never carried by colour alone.
- **Do** keep one active edge: a 2px Print Black outline at 2px offset on every focusable element, never removed.
- **Do** mark the current item (nav page, payment station, reading row) with ink and a 2px ink underline.

### Don't:
- **Don't** use cards, rounded corners, drop shadows, sidebars, KPI tiles or status pills. The inset 2px underline is a printed rule and is the only shadow value allowed.
- **Don't** print a refusal that is the system working ("not yours to decide", "already decided") in red.
- **Don't** set a small label above a title; the title says what the page is.
- **Don't** set column titles in the mono, even over figures.
- **Don't** add motion beyond the reading ruler.
- **Don't** make the agent's escalation look authoritative: no fill, no emphasis colour, no border heavier than the dashed perforation.
