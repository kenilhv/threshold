"""US service-minimal invoice layout.

Modelled on the invoice a mid-sized American consultancy, design studio, or law
practice sends: a wordmark, an ocean of whitespace, no ruled table anywhere, an
oversized right-aligned "Amount Due", hours-based line items billed against an
hourly rate, and Net-30 terms in small grey type at the foot of the page. Think
the default template a services firm builds once in InDesign and then uses for
eight years.

Extraction failure modes this family exercises on purpose
---------------------------------------------------------
1.  **No table rulings whatsoever.** Column structure exists only through
    alignment and consistent baselines. Any extractor that finds columns by
    detecting drawn lines, cell borders, or shaded header bands sees a single
    unstructured text block and has to fall back on x-coordinate clustering.

2.  **A hero figure that is not the only currency figure.** "Amount Due" is set
    at roughly three times body size and is the most visually dominant element
    on the page, but the subtotal and the tax amount sit directly above it in
    8pt grey, inside the same right-aligned column. A reader keying on "largest
    numeric value on the page" is wrong on every credit note (where the
    subtotal exceeds the negative total), and a reader keying on "figure nearest
    the biggest type" has three candidates within 40 points of each other.

3.  **Non-integer quantities.** Hours are billed as decimals — 7.25, 11.50,
    0.75 — so any quantity parser assuming an integer count of goods produces
    either a parse failure or, worse, a silent truncation to 7.

4.  **A decoy purchase-order field.** The metadata block always carries a
    project/reference/engagement code shaped exactly like a purchase order
    ("PO-2026-4417", "4501883204"), sitting under a label that is emphatically
    not "Purchase Order". The *real* PO number, when the spec has one at all,
    is deliberately placed far away — down in the remittance fine print or in
    the notes column — and roughly a third of specs carry no PO at all. This is
    the single most confident mistake a document extractor makes in accounts
    payable, and the golden set is worthless if it does not contain it.

5.  **Bare currency symbol.** Amounts render as "$12,480.00", never "USD
    12,480.00". The ISO code appears exactly once, in the remittance line at
    the very bottom of the page, which is where a real services invoice puts it
    and nowhere near the figure a naive extractor reads.

6.  **Unit of measure fused into the quantity cell.** "7.25 HR" is one visual
    token in a right-aligned column, so quantity and unit have to be split by
    the reader rather than read from separate columns.

Determinism
-----------
Every variable choice — decoy label and format, decoy digits, where the real PO
lands, the wording of the hero label and the closing line — is drawn from a
local ``random.Random(spec.seed)``. The canvas is opened with ``invariant=1``
and its document metadata is pinned, so reportlab embeds neither a wall-clock
creation date nor a random document id. Same spec in, byte-identical PDF out.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas as rl_canvas  # type: ignore[import-untyped]

from seeds.invoice_spec import InvoiceSpec, LineItem

LAYOUT_NAME: Final[str] = "us_service_minimal"

__all__ = ["LAYOUT_NAME", "render"]

# --------------------------------------------------------------------------
# Page geometry. US Letter, one-inch-ish margins, and a lot of air.
# --------------------------------------------------------------------------
_PAGE_W: Final[float] = float(letter[0])
_PAGE_H: Final[float] = float(letter[1])

_LEFT: Final[float] = 64.0
_RIGHT: Final[float] = _PAGE_W - 64.0
_TOP: Final[float] = _PAGE_H - 64.0

#: Right-aligned money columns. No rules are ever drawn between them.
_COL_QTY_R: Final[float] = 396.0
_COL_RATE_R: Final[float] = 468.0
_COL_AMOUNT_R: Final[float] = _RIGHT
_COL_DESC_W: Final[float] = 268.0

#: Right-hand metadata column of the header block.
_META_LABEL_X: Final[float] = 356.0

_REG: Final[str] = "Helvetica"
_BOLD: Final[str] = "Helvetica-Bold"

_INK: Final[tuple[float, float, float]] = (0.09, 0.09, 0.10)
_MUTED: Final[tuple[float, float, float]] = (0.42, 0.43, 0.45)
_FAINT: Final[tuple[float, float, float]] = (0.60, 0.61, 0.63)

#: Vertical room the totals hero needs beneath the last line item.
_HERO_H: Final[float] = 142.0

#: The letterspaced column-head band, which costs the same on every page.
_HEADS_H: Final[float] = 20.0

_HOUR_UNITS: Final[frozenset[str]] = frozenset(
    {"HR", "HRS", "HOUR", "HOURS", "H", "DAY", "DAYS", "WK", "WEEK", "WEEKS"}
)

#: Deliberately narrow. A US services layout does not localise; it prints a
#: symbol and states the ISO code once in the remittance fine print.
_SYMBOLS: Final[dict[str, str]] = {
    "USD": "$",
    "CAD": "$",
    "AUD": "$",
    "NZD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
}

_DECOY_LABELS: Final[tuple[str, ...]] = ("Project", "Reference", "Engagement", "Client Ref.")
_HERO_LABELS: Final[tuple[str, ...]] = ("Amount Due", "Total Due", "Balance Due")
_CLOSINGS: Final[tuple[str, ...]] = (
    "Thank you for the continued partnership.",
    "Thank you for your business.",
    "We appreciate the opportunity to work with you.",
)


# --------------------------------------------------------------------------
# Formatting. Decimal in, str out, and no float anywhere on the path.
# --------------------------------------------------------------------------
def _symbol(currency: str) -> str:
    return _SYMBOLS.get(currency.upper(), currency.upper() + " ")


def _money(amount: Decimal, symbol: str) -> str:
    quantised = amount.quantize(Decimal("0.01"))
    sign = "-" if quantised < 0 else ""
    return f"{sign}{symbol}{format(abs(quantised), ',.2f')}"


def _percent(rate: Decimal) -> str:
    """Render a tax rate, tolerating either the 0.0875 or the 8.75 convention.

    Both appear in the wild and the spec does not pin one down. A rate at or
    below 1 is read as a fraction; anything above is already a percentage. No
    real sales-tax rate is ambiguous under that rule.
    """
    pct = rate * Decimal(100) if abs(rate) <= 1 else rate
    text = format(pct.quantize(Decimal("0.001")).normalize(), "f")
    if "E" in text or "e" in text:  # normalize() can produce exponent form
        text = format(pct.quantize(Decimal("0.001")), "f").rstrip("0").rstrip(".")
    return f"{text}%"


def _quantity(item: LineItem) -> str:
    """Hours keep two decimals; counted goods lose a pointless ``.00``."""
    if item.unit_of_measure.strip().upper() in _HOUR_UNITS:
        return format(item.quantity.quantize(Decimal("0.01")), ",.2f")
    if item.quantity == item.quantity.to_integral_value():
        # ",d" is an integer presentation type and Decimal rejects it. Convert
        # first; the value is already known to be integral at this branch.
        return format(int(item.quantity.quantize(Decimal("1"))), ",d")
    return format(item.quantity.quantize(Decimal("0.01")), ",.2f")


def _us_date(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _text_width(text: str, font: str, size: float, char_space: float = 0.0) -> float:
    width: float = float(pdfmetrics.stringWidth(text, font, size))
    if char_space:
        width += char_space * max(len(text) - 1, 0)
    return width


def _wrap(text: str, font: str, size: float, max_w: float) -> list[str]:
    """Greedy word wrap. Hard-splits any single word wider than the column."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if _text_width(candidate, font, size) <= max_w or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)

    out: list[str] = []
    for line in lines:
        if _text_width(line, font, size) <= max_w:
            out.append(line)
            continue
        chunk = ""
        for char in line:
            if chunk and _text_width(chunk + char, font, size) > max_w:
                out.append(chunk)
                chunk = char
            else:
                chunk += char
        if chunk:
            out.append(chunk)
    return out or [""]


# --------------------------------------------------------------------------
# Drawing primitives.
# --------------------------------------------------------------------------
def _draw(
    c: rl_canvas.Canvas,
    x: float,
    y: float,
    text: str,
    font: str,
    size: float,
    colour: tuple[float, float, float],
    *,
    right: bool = False,
    char_space: float = 0.0,
) -> None:
    if not text:
        return
    start = x - _text_width(text, font, size, char_space) if right else x
    if not char_space:
        c.setFont(font, size)
        c.setFillColorRGB(*colour)
        c.drawString(start, y, text)
        return
    # Letterspacing lives on the text object, not the canvas, in reportlab.
    obj = c.beginText(start, y)
    obj.setFont(font, size)
    obj.setFillColorRGB(*colour)
    obj.setCharSpace(char_space)
    obj.textOut(text)
    obj.setCharSpace(0.0)
    c.drawText(obj)


def _draw_eyebrow(
    c: rl_canvas.Canvas,
    x: float,
    y: float,
    text: str,
    *,
    right: bool = False,
    size: float = 6.8,
) -> None:
    """The letterspaced all-caps micro-label this family uses instead of rules."""
    _draw(c, x, y, text.upper(), _BOLD, size, _FAINT, right=right, char_space=1.25)


# --------------------------------------------------------------------------
# Seeded variation.
# --------------------------------------------------------------------------
def _decoy_reference(rng: random.Random, spec: InvoiceSpec) -> tuple[str, str]:
    """A project code shaped like a purchase order. It is never the real PO."""
    year = spec.issue_date.year
    label = rng.choice(_DECOY_LABELS)
    shape = rng.randrange(5)
    for _ in range(8):
        if shape == 0:
            value = f"PO-{year}-{rng.randrange(1000, 10000)}"
        elif shape == 1:
            value = f"{rng.randrange(4500, 4600)}{rng.randrange(100000, 1000000)}"
        elif shape == 2:
            value = f"PRJ-{rng.randrange(10000, 100000)}"
        elif shape == 3:
            prefix = rng.choice(("AC", "MK", "DS", "EN", "ST"))
            value = f"{prefix}-{year % 100}{rng.randrange(100, 1000)}"
        else:
            value = f"P{rng.randrange(100000, 1000000)}"
        if spec.po_number is None or value != spec.po_number:
            return label, value
    return label, f"{value}-R"


def _hours_billed(spec: InvoiceSpec) -> Decimal | None:
    total = Decimal(0)
    seen = False
    for item in spec.line_items:
        if item.unit_of_measure.strip().upper() in _HOUR_UNITS:
            total += item.quantity
            seen = True
    return total if seen else None


# --------------------------------------------------------------------------
# Pre-layout: line-item blocks and footer, measured before anything is drawn
# so that page counts are known up front.
# --------------------------------------------------------------------------
class _Row:
    """One measured line item, wrapped and ready to draw."""

    __slots__ = ("amount", "desc_lines", "height", "qty", "rate", "sku")

    def __init__(self, item: LineItem, symbol: str) -> None:
        self.desc_lines: list[str] = _wrap(item.description, _REG, 9.5, _COL_DESC_W)
        self.sku: str | None = item.sku
        self.qty: str = f"{_quantity(item)} {item.unit_of_measure.strip().upper()}".strip()
        self.rate: str = _money(item.unit_price, symbol)
        self.amount: str = _money(item.line_total, symbol)
        self.height: float = 13.0 * len(self.desc_lines) + (10.0 if self.sku else 0.0) + 12.0


class _Footer:
    """The fine print, measured before pagination so floors can be derived."""

    __slots__ = ("fine", "height", "note_lines", "po_line")

    def __init__(self, spec: InvoiceSpec, rng: random.Random) -> None:
        self.note_lines: list[str] = (
            _wrap(spec.notes.strip(), _REG, 8.5, _RIGHT - _LEFT) if spec.notes else []
        )
        self.po_line: str | None = None
        self.fine: list[str] = []

        remit = (
            f"Remit {spec.currency.upper()} by ACH · routing ending "
            f"{spec.bank_routing_last4} · account ending {spec.bank_iban_last4}"
        )
        identity = f"{spec.vendor_name} · Tax ID {spec.vendor_tax_id}"

        po_placement = rng.randrange(2) if spec.po_number else -1
        po_sentence = (
            f"Services rendered under purchase order {spec.po_number}." if spec.po_number else None
        )
        if po_placement == 0 and po_sentence:
            self.fine.append(po_sentence)
        elif po_placement == 1 and po_sentence:
            self.po_line = po_sentence

        self.fine.append(remit)
        self.fine.append(identity)
        self.fine.append(rng.choice(_CLOSINGS))

        height = 11.5 * len(self.note_lines)
        if self.note_lines:
            height += 20.0
        if self.po_line:
            height += 16.0
        height += 11.0 * len(self.fine) + 18.0
        self.height: float = height


def _meta_rows(spec: InvoiceSpec, decoy: tuple[str, str]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = [
        ("Issued", _us_date(spec.issue_date)),
        ("Due", _us_date(spec.due_date)),
        ("Terms", spec.payment_terms),
        (decoy[0], decoy[1]),
    ]
    hours = _hours_billed(spec)
    if hours is not None:
        rows.append(("Hours", format(hours.quantize(Decimal("0.01")), ",.2f")))
    return rows


def _header_block_top(spec: InvoiceSpec) -> float:
    """Baseline of the BILLED TO / DETAILS eyebrows on the first page."""
    after_address = _TOP - 17.0 - 11.0 * len(spec.vendor_address_lines)
    return min(after_address - 44.0, _TOP - 96.0)


def _first_items_top(spec: InvoiceSpec, meta: list[tuple[str, str]]) -> float:
    """Where line items start on page one.

    Pagination and drawing both call this, so the measured layout and the
    rendered layout can never disagree about how much room the header took.
    """
    block_top = _header_block_top(spec)
    billed_bottom = block_top - 16.0 - 13.0 * len(spec.bill_to_lines)
    meta_bottom = block_top - 16.0 - 13.5 * len(meta)
    return min(billed_bottom, meta_bottom) - 40.0


def _page_capacity_top(pages: list[list[_Row]], first_top: float, cont_top: float) -> float:
    return first_top if len(pages) <= 1 else cont_top


def _paginate(
    rows: list[_Row],
    first_top: float,
    cont_top: float,
    floor: float,
) -> list[list[_Row]]:
    """Split rows across pages, then guarantee the totals hero has room.

    The column-head band costs 20pt on every page, and the hero has to clear
    the footer on the last one. If the tail of the run cannot do both, rows are
    pushed forward a page rather than the hero being allowed to collide.
    """
    pages: list[list[_Row]] = []
    current: list[_Row] = []
    y = first_top - _HEADS_H
    for row in rows:
        if current and y - row.height < floor:
            pages.append(current)
            current = []
            y = cont_top - _HEADS_H
        current.append(row)
        y -= row.height

    pages.append(current)

    for _ in range(len(rows) + 1):
        top = _page_capacity_top(pages, first_top, cont_top)
        end = top - _HEADS_H - sum(row.height for row in pages[-1])
        if end - _HERO_H >= floor:
            break
        if len(pages[-1]) > 1:
            pages.append([pages[-1].pop()])
            continue
        if pages[-1]:
            # A single row plus the hero still will not fit. Give the hero a
            # page of its own; generous whitespace is this family's whole
            # personality, so a sparse final page reads as intent, not defect.
            pages.append([])
        break

    return pages


# --------------------------------------------------------------------------
# Page furniture.
# --------------------------------------------------------------------------
def _draw_first_page_header(
    c: rl_canvas.Canvas,
    spec: InvoiceSpec,
    meta: list[tuple[str, str]],
) -> float:
    y = _TOP

    _draw(c, _LEFT, y, spec.vendor_name, _REG, 19.0, _INK)
    _draw_eyebrow(c, _RIGHT, y + 4.0, "Invoice", right=True, size=7.2)
    _draw(c, _RIGHT, y - 13.0, spec.invoice_number, _REG, 12.5, _INK, right=True)

    y -= 17.0
    for line in spec.vendor_address_lines:
        _draw(c, _LEFT, y, line, _REG, 8.5, _MUTED)
        y -= 11.0

    block_top = _header_block_top(spec)

    # Left: billed-to. Right: the metadata stack, including the decoy code.
    _draw_eyebrow(c, _LEFT, block_top, "Billed to")
    yb = block_top - 16.0
    for line in spec.bill_to_lines:
        _draw(c, _LEFT, yb, line, _REG, 9.5, _INK)
        yb -= 13.0

    ym = block_top
    _draw_eyebrow(c, _META_LABEL_X, ym, "Details")
    ym -= 16.0
    for label, value in meta:
        _draw(c, _META_LABEL_X, ym, label, _REG, 8.5, _MUTED)
        _draw(c, _RIGHT, ym, value, _REG, 9.5, _INK, right=True)
        ym -= 13.5

    return _first_items_top(spec, meta)


def _draw_continuation_header(c: rl_canvas.Canvas, spec: InvoiceSpec) -> float:
    _draw(c, _LEFT, _TOP, spec.vendor_name, _REG, 11.0, _INK)
    _draw(
        c,
        _RIGHT,
        _TOP,
        f"Invoice {spec.invoice_number} · continued",
        _REG,
        8.5,
        _MUTED,
        right=True,
    )
    return _TOP - 44.0


def _draw_column_heads(c: rl_canvas.Canvas, y: float) -> float:
    _draw_eyebrow(c, _LEFT, y, "Description")
    _draw_eyebrow(c, _COL_QTY_R, y, "Qty", right=True)
    _draw_eyebrow(c, _COL_RATE_R, y, "Rate", right=True)
    _draw_eyebrow(c, _COL_AMOUNT_R, y, "Amount", right=True)
    return y - _HEADS_H


def _draw_rows(c: rl_canvas.Canvas, rows: list[_Row], y: float) -> float:
    for row in rows:
        _draw(c, _COL_QTY_R, y, row.qty, _REG, 9.5, _INK, right=True)
        _draw(c, _COL_RATE_R, y, row.rate, _REG, 9.5, _INK, right=True)
        _draw(c, _COL_AMOUNT_R, y, row.amount, _REG, 9.5, _INK, right=True)
        for line in row.desc_lines:
            _draw(c, _LEFT, y, line, _REG, 9.5, _INK)
            y -= 13.0
        if row.sku:
            _draw(c, _LEFT, y + 1.5, row.sku, _REG, 7.5, _FAINT)
            y -= 10.0
        y -= 12.0
    return y


def _draw_hero(c: rl_canvas.Canvas, spec: InvoiceSpec, rng: random.Random, y: float) -> None:
    symbol = _symbol(spec.currency)

    y -= 22.0
    _draw(c, _COL_RATE_R, y, "Subtotal", _REG, 8.0, _MUTED, right=True)
    _draw(c, _COL_AMOUNT_R, y, _money(spec.subtotal, symbol), _REG, 8.5, _MUTED, right=True)

    y -= 13.0
    _draw(c, _COL_RATE_R, y, f"Tax ({_percent(spec.tax_rate)})", _REG, 8.0, _MUTED, right=True)
    _draw(c, _COL_AMOUNT_R, y, _money(spec.tax_amount, symbol), _REG, 8.5, _MUTED, right=True)

    y -= 30.0
    label = rng.choice(_HERO_LABELS)
    if spec.is_credit_note:
        label = "Credit Due"
    _draw_eyebrow(c, _COL_AMOUNT_R, y, label, right=True, size=7.2)

    y -= 32.0
    _draw(c, _COL_AMOUNT_R, y, _money(spec.total, symbol), _REG, 30.0, _INK, right=True)

    y -= 15.0
    _draw(
        c, _COL_AMOUNT_R, y, f"Payable by {_us_date(spec.due_date)}", _REG, 8.0, _MUTED, right=True
    )


def _draw_footer(c: rl_canvas.Canvas, footer: _Footer, top: float) -> float:
    """Draw the fine print and return the baseline of its last line."""
    y = top
    if footer.note_lines:
        _draw_eyebrow(c, _LEFT, y, "Notes")
        y -= 14.0
        for line in footer.note_lines:
            _draw(c, _LEFT, y, line, _REG, 8.5, _MUTED)
            y -= 11.5
        y -= 6.0
    if footer.po_line:
        _draw(c, _LEFT, y, footer.po_line, _REG, 8.5, _MUTED)
        y -= 16.0
    y -= 12.0
    last = y
    for line in footer.fine:
        _draw(c, _LEFT, y, line, _REG, 7.5, _FAINT)
        last = y
        y -= 11.0
    return last


# --------------------------------------------------------------------------
# Public entry point.
# --------------------------------------------------------------------------
def render(spec: InvoiceSpec, path: Path) -> None:
    """Render ``spec`` as a US service-minimal invoice PDF at ``path``.

    The spec is read only. Nothing on this path mutates it, uses a float for
    money, or consults the clock, the global RNG, or the network.
    """
    rng = random.Random(spec.seed)
    symbol = _symbol(spec.currency)

    decoy = _decoy_reference(rng, spec)
    meta = _meta_rows(spec, decoy)
    footer = _Footer(spec, rng)
    rows = [_Row(item, symbol) for item in spec.line_items]

    floor = 40.0 + footer.height
    first_items_top = _first_items_top(spec, meta)
    cont_items_top = _TOP - 64.0
    pages = _paginate(rows, first_items_top, cont_items_top, floor)

    path.parent.mkdir(parents=True, exist_ok=True)

    c = rl_canvas.Canvas(
        str(path),
        pagesize=(_PAGE_W, _PAGE_H),
        invariant=1,
        pageCompression=1,
    )
    # reportlab otherwise stamps a wall-clock CreationDate and a random file id
    # into the trailer, which would make byte-identical output impossible.
    c.setTitle(f"Invoice {spec.invoice_number}")
    c.setAuthor(spec.vendor_name)
    c.setSubject(LAYOUT_NAME)
    c.setCreator(LAYOUT_NAME)
    c.setProducer("threshold-seeds")
    c.setKeywords(LAYOUT_NAME)

    total_pages = len(pages)
    for index, page_rows in enumerate(pages):
        if index == 0:
            y = _draw_first_page_header(c, spec, meta)
        else:
            y = _draw_continuation_header(c, spec)
        if page_rows:
            y = _draw_column_heads(c, y)
            y = _draw_rows(c, page_rows, y)
        else:
            y -= _HEADS_H

        marker_y = 40.0
        if index == total_pages - 1:
            _draw_hero(c, spec, rng, y)
            # Sit the marker on the fine print's own last baseline so it can
            # never drift into a long notes block on a crowded final page.
            marker_y = _draw_footer(c, footer, floor - 8.0)

        if total_pages > 1:
            _draw(
                c,
                _RIGHT,
                marker_y,
                f"Page {index + 1} of {total_pages}",
                _REG,
                7.5,
                _FAINT,
                right=True,
            )
        c.showPage()

    c.save()
