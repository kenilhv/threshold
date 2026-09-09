"""Multi-page continuation documents and credit notes.

Modelled on the invoice output of mid-market ERP suites — NetSuite, Sage
Intacct, Microsoft Dynamics BC and the older SAP SmartForms templates. Those
systems all share a house style for documents whose line detail does not fit on
one page: a full masthead on page one, a compressed running header afterwards,
the column header band repeated on every page, a "brought forward" figure at
the top of each continuation page, a "carried forward" figure and a continued
marker at the foot of every page that is not the last, and the totals block
printed exactly once, on the final page. The credit-note variant is the same
document with the title changed, the amount column relabelled to its credit
sense, an original-invoice reference in the masthead, and negative money shown
in accounting brackets instead of with a minus sign.

Extraction failure modes this family exercises
----------------------------------------------
1.  **Totals absent from page one.** An extractor that reads only the first
    page — or that stops at the first page because it found a header there —
    finds no subtotal, no tax and no total. The only totals block in the
    document is on the last page.
2.  **Repeated column header.** The header band is re-emitted on every page
    with the same column geometry as a line item. There is no line-number
    column, so the header row is structurally indistinguishable from a data
    row to anything that segments the table by horizontal rules alone. Naive
    table extraction yields a phantom item whose description is "Description"
    and whose quantity is "Qty".
3.  **Brought/carried forward rows.** Pages two and later open with a
    "Brought forward from page N" row that carries a money amount in the
    Amount column, and every non-final page closes with the matching "Carried
    forward" row. These are running totals, not line items. Summing the Amount
    column naively double-counts every earlier page.
4.  **Bracketed negatives.** On a credit note every negative figure renders as
    ``(1.234,56)`` rather than ``-1234.56``. A parser that strips
    non-numeric characters reads the credit as a positive charge, which is the
    single most expensive parse error in accounts payable.
5.  **Non-"INVOICE" document title.** The masthead reads "CREDIT NOTE" and the
    money column reads "Credit" rather than "Amount". Document-type
    classifiers trained on the word INVOICE mislabel it, and downstream
    three-way matching then tries to match a credit against a purchase order.

A sixth mode falls out of the first: a line item whose description is too tall
for the remaining page is split, and the continuation slice on the next page is
description text with no code, quantity or amount beside it. Extractors that
require every table row to carry a money value drop it.

Determinism
-----------
Every stochastic choice is drawn from a ``random.Random(spec.seed)`` created
in this module, in a fixed order, before any layout work begins — so the draw
sequence cannot be perturbed by pagination. reportlab's creation timestamp and
document id are suppressed with ``invariant=True`` and the producer string is
pinned, so the same spec renders to the same bytes on any machine with the
same reportlab build.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

from reportlab.lib.colors import Color, HexColor  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4, LETTER  # type: ignore[import-untyped]
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from seeds.invoice_spec import InvoiceSpec, LineItem

__all__ = ["LAYOUT_NAME", "render"]

LAYOUT_NAME: Final[str] = "multipage_credit"

# --------------------------------------------------------------------------
# Palette and metrics. Laser-printer black on white, one grey for rules and
# one for de-emphasised label text. ERP output is not a design exercise.
# --------------------------------------------------------------------------
_INK: Final[Color] = HexColor("#141414")
_MUTED: Final[Color] = HexColor("#5a5a5a")
_RULE: Final[Color] = HexColor("#9a9a9a")
_HAIRLINE: Final[Color] = HexColor("#d5d5d5")
_BAND: Final[Color] = HexColor("#ececec")

_BODY: Final[str] = "Helvetica"
_BOLD: Final[str] = "Helvetica-Bold"
_ITALIC: Final[str] = "Helvetica-Oblique"
_MONO: Final[str] = "Courier"

_BODY_SIZE: Final[float] = 8.5
_BODY_LEADING: Final[float] = 10.5
_SMALL_SIZE: Final[float] = 7.2

_MARGIN_X: Final[float] = 18.0 * mm
_MARGIN_TOP: Final[float] = 15.0 * mm
_MARGIN_BOTTOM: Final[float] = 15.0 * mm

_CELL_PAD: Final[float] = 3.5
_ROW_PAD: Final[float] = 3.5

#: Width of the right-hand label/value grid in the masthead.
_META_WIDTH: Final[float] = 205.0
#: Width of the totals block on the final page.
_TOTALS_WIDTH: Final[float] = 185.0

_COLHDR_H: Final[float] = 17.0
_BROUGHT_H: Final[float] = 20.0
_CONT_FOOT_H: Final[float] = 30.0
_FOOTER_H: Final[float] = 24.0

# Currencies with no minor unit, and currencies conventionally printed with a
# comma decimal separator. Both lists drive formatting only.
_ZERO_DECIMAL: Final[frozenset[str]] = frozenset({"JPY", "KRW", "CLP", "ISK", "VND", "HUF"})
_COMMA_DECIMAL: Final[frozenset[str]] = frozenset(
    {"EUR", "DKK", "SEK", "NOK", "PLN", "CZK", "RON", "TRY", "BRL", "CHF"}
)
_LETTER_CURRENCIES: Final[frozenset[str]] = frozenset({"USD", "CAD", "MXN"})

_CONTINUED_PHRASES: Final[tuple[str, ...]] = (
    "Continued on page {next} — this document has {total} pages",
    "*** CONTINUED — see page {next} of {total} ***",
    "Document continues on page {next}. Do not detach.",
)
_BROUGHT_LABELS: Final[tuple[str, ...]] = (
    "Brought forward from page {prev}",
    "Balance brought forward (page {prev})",
    "Subtotal b/f from page {prev}",
)
_CARRIED_LABELS: Final[tuple[str, ...]] = (
    "Carried forward to page {next}",
    "Subtotal c/f to page {next}",
    "Balance carried forward",
)
_BOILERPLATE: Final[tuple[str, ...]] = (
    "Goods remain the property of the supplier until paid for in full.",
    "Please quote the document number above on all remittances and queries.",
    "Interest is chargeable on overdue balances at the statutory rate.",
)


# --------------------------------------------------------------------------
# Formatting helpers. Money is Decimal until the last possible moment.
# --------------------------------------------------------------------------
def _separators(currency: str) -> tuple[str, str]:
    """Return (thousands, decimal) separators for ``currency``."""
    if currency.upper() in _COMMA_DECIMAL:
        return ".", ","
    return ",", "."


def _grouped(value: Decimal, currency: str, places: int | None) -> str:
    """Format ``abs(value)`` with thousands grouping, no sign."""
    if places is None:
        magnitude = value.copy_abs().normalize()
        if magnitude == magnitude.to_integral_value():
            magnitude = magnitude.quantize(Decimal(1))
    else:
        magnitude = value.copy_abs().quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(magnitude, "f")
    whole, _, frac = text.partition(".")
    groups: list[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    thousands, decimal_point = _separators(currency)
    out = thousands.join(groups)
    if frac:
        out = f"{out}{decimal_point}{frac}"
    return out


def _amount(value: Decimal, currency: str) -> str:
    """Money as this document prints it: negatives in accounting brackets."""
    places = 0 if currency.upper() in _ZERO_DECIMAL else 2
    text = _grouped(value, currency, places)
    return f"({text})" if value < 0 else text


def _quantity(value: Decimal, currency: str) -> str:
    return _grouped(value, currency, None) if value >= 0 else f"({_grouped(value, currency, None)})"


def _percent(rate: Decimal) -> str:
    """Render a tax rate, accepting either a fraction (0.19) or points (19)."""
    points = rate * 100 if rate.copy_abs() < 1 else rate
    trimmed = points.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP).normalize()
    if trimmed == trimmed.to_integral_value():
        trimmed = trimmed.quantize(Decimal(1))
    return f"{format(trimmed, 'f')}%"


def _date(value: date) -> str:
    """Unambiguous across locales, which is what ERP output actually prints."""
    return f"{value.day:02d} {value.strftime('%b')} {value.year}"


def _wrap(text: str, font: str, size: float, width: float) -> tuple[str, ...]:
    """Greedy word wrap, hard-breaking any single word wider than the column."""
    if not text:
        return ("",)
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and pdfmetrics.stringWidth(candidate, font, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
        while pdfmetrics.stringWidth(current, font, size) > width and len(current) > 1:
            cut = len(current)
            while cut > 1 and pdfmetrics.stringWidth(current[:cut], font, size) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current or not lines:
        lines.append(current)
    return tuple(lines)


def _fit(text: str, font: str, size: float, width: float) -> str:
    """Single-line truncation with an ellipsis, for narrow code columns."""
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    cut = len(text)
    while cut > 0 and pdfmetrics.stringWidth(f"{text[:cut]}…", font, size) > width:
        cut -= 1
    return f"{text[:cut]}…"


def _derive_original_reference(invoice_number: str, rng: random.Random) -> str:
    """Invent the invoice this credit note reverses, stably from the seed.

    The spec carries no field for it, so the reference is derived: the trailing
    numeric run of the credit note's own number is walked back by a seeded
    offset and any credit-note prefix is rewritten to INV. That is how a real
    ledger looks — the credit is issued shortly after the invoice it reverses.
    """
    offset = rng.randrange(1, 40)
    fallback = rng.randrange(100000, 999999)
    match = re.search(r"(\d+)(\D*)$", invoice_number)
    if match is None:
        return f"INV-{fallback}"
    digits = match.group(1)
    sequence = int(digits) - offset
    if sequence < 1:
        sequence = int(digits) + offset
    body = invoice_number[: match.start(1)] + str(sequence).zfill(len(digits)) + match.group(2)
    for prefix in ("CRN", "CN", "CR"):
        if body.upper().startswith(prefix):
            return "INV" + body[len(prefix) :]
    return body


# --------------------------------------------------------------------------
# Table geometry
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Column:
    key: str
    label: str
    width: float
    align: str  # "l", "c" or "r"


@dataclass(frozen=True, slots=True)
class _Row:
    item: LineItem
    desc_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Slice:
    """A contiguous run of one line item's wrapped description lines.

    A description long enough to outgrow a page is broken across pages, which
    is what an ERP does. Only the first slice of an item carries its code,
    quantity and money — the continuation slice is bare description text, a
    fragment with no amount, and is another thing a table extractor has to get
    right.
    """

    row: int
    start: int
    end: int

    @property
    def first(self) -> bool:
        return self.start == 0

    def height(self) -> float:
        return (self.end - self.start) * _BODY_LEADING + _ROW_PAD


class _Renderer:
    """One document. Holds no state that outlives :meth:`run`."""

    def __init__(self, spec: InvoiceSpec, path: Path) -> None:
        self.spec = spec
        self.path = path
        self.credit = spec.is_credit_note
        self.currency = spec.currency.upper()

        rng = random.Random(spec.seed)
        # Every draw happens here, in a fixed order, before any layout runs.
        self.doc_control = f"{rng.randrange(0x1000, 0x10000):04X}-{rng.randrange(0x10, 0x100):02X}"
        self.continued_phrase = _CONTINUED_PHRASES[rng.randrange(len(_CONTINUED_PHRASES))]
        self.brought_label = _BROUGHT_LABELS[rng.randrange(len(_BROUGHT_LABELS))]
        self.carried_label = _CARRIED_LABELS[rng.randrange(len(_CARRIED_LABELS))]
        self.boilerplate = _BOILERPLATE[rng.randrange(len(_BOILERPLATE))]
        derived_original = _derive_original_reference(spec.invoice_number, rng)
        self.original_reference = str(
            spec.expected_fields.get("original_invoice_number")
            or spec.expected_fields.get("original_invoice")
            or derived_original
        )

        page_size = LETTER if self.currency in _LETTER_CURRENCIES else A4
        self.page_w = float(page_size[0])
        self.page_h = float(page_size[1])
        self.left = _MARGIN_X
        self.right = self.page_w - _MARGIN_X
        self.top = self.page_h - _MARGIN_TOP
        self.bottom = _MARGIN_BOTTOM

        self.columns = self._build_columns()
        self.col_x: dict[str, tuple[float, float]] = {}
        cursor = self.left
        for column in self.columns:
            self.col_x[column.key] = (cursor, column.width)
            cursor += column.width
        self.align = {column.key: column.align for column in self.columns}

        self.canvas = Canvas(
            str(path),
            pagesize=(self.page_w, self.page_h),
            invariant=True,
            pageCompression=1,
        )
        title = "Credit note" if self.credit else "Invoice"
        self.canvas.setTitle(f"{title} {spec.invoice_number}")
        self.canvas.setAuthor(spec.vendor_name)
        self.canvas.setSubject(f"{title} for {spec.bill_to_lines[0] if spec.bill_to_lines else ''}")
        self.canvas.setCreator("Threshold fixture generator")
        self.canvas.setProducer(f"threshold-seeds/{LAYOUT_NAME}")

    # -- geometry ---------------------------------------------------------
    def _build_columns(self) -> tuple[_Column, ...]:
        available = self.right - self.left
        has_sku = any(item.sku for item in self.spec.line_items)
        amount_label = f"Credit {self.currency}" if self.credit else f"Amount {self.currency}"
        fixed: list[_Column] = []
        if has_sku:
            fixed.append(_Column("sku", "Item code", 68.0, "l"))
        tail = (
            _Column("uom", "UM", 32.0, "c"),
            _Column("qty", "Qty", 48.0, "r"),
            _Column("unit", f"Unit price {self.currency}", 68.0, "r"),
            _Column("amount", amount_label, 82.0, "r"),
        )
        used = sum(column.width for column in fixed) + sum(column.width for column in tail)
        description = _Column("description", "Description", max(120.0, available - used), "l")
        return (*fixed, description, *tail)

    def _desc_width(self) -> float:
        _, width = self.col_x["description"]
        return width - 2 * _CELL_PAD

    # -- drawing primitives ----------------------------------------------
    def _text(
        self,
        canvas: Canvas,
        x: float,
        y: float,
        value: str,
        font: str,
        size: float,
        color: Color,
    ) -> None:
        canvas.setFont(font, size)
        canvas.setFillColor(color)
        canvas.drawString(x, y, value)

    def _right_text(
        self,
        canvas: Canvas,
        x: float,
        y: float,
        value: str,
        font: str,
        size: float,
        color: Color,
    ) -> None:
        canvas.setFont(font, size)
        canvas.setFillColor(color)
        canvas.drawRightString(x, y, value)

    def _rule(self, canvas: Canvas, y: float, width: float, color: Color) -> None:
        canvas.setStrokeColor(color)
        canvas.setLineWidth(width)
        canvas.line(self.left, y, self.right, y)

    def _cell(
        self,
        canvas: Canvas,
        key: str,
        y: float,
        value: str,
        font: str,
        size: float,
        color: Color,
    ) -> None:
        x, width = self.col_x[key]
        canvas.setFont(font, size)
        canvas.setFillColor(color)
        align = self.align[key]
        if align == "r":
            canvas.drawRightString(x + width - _CELL_PAD, y, value)
        elif align == "c":
            canvas.drawCentredString(x + width / 2.0, y, value)
        else:
            canvas.drawString(x + _CELL_PAD, y, value)

    # -- blocks. Each measures when ``canvas`` is None and draws otherwise. --
    def _masthead(self, y_top: float, canvas: Canvas | None) -> float:
        spec = self.spec
        title = "CREDIT NOTE" if self.credit else "INVOICE"

        meta: list[tuple[str, str]] = [
            ("Credit note no." if self.credit else "Invoice no.", spec.invoice_number),
            ("Issue date", _date(spec.issue_date)),
            ("Due date", _date(spec.due_date)),
        ]
        if self.credit:
            meta.append(("Original invoice", self.original_reference))
        meta.append(("PO number", spec.po_number if spec.po_number else "—"))
        meta.append(("Payment terms", spec.payment_terms))
        meta.append(("Currency", self.currency))

        left_h = 14.0 + len(spec.vendor_address_lines) * 9.6 + 4.0 + 9.6
        right_h = 22.0 + 6.0 + len(meta) * 11.0
        block_h = max(left_h, right_h)

        billto_label = "CREDIT TO" if self.credit else "BILL TO"
        billto_h = 9.0 + 3.0 + len(spec.bill_to_lines) * 10.2
        total_h = block_h + 13.0 + billto_h + 12.0

        if canvas is None:
            return total_h

        # The left column must not run under the meta grid on the right.
        left_room = self.right - _META_WIDTH - self.left - 12.0
        y = y_top - 11.0
        self._text(
            canvas, self.left, y, _fit(spec.vendor_name, _BOLD, 12.5, left_room), _BOLD, 12.5, _INK
        )
        y -= 12.0
        for line in spec.vendor_address_lines:
            self._text(
                canvas,
                self.left,
                y,
                _fit(line, _BODY, _SMALL_SIZE, left_room),
                _BODY,
                _SMALL_SIZE,
                _MUTED,
            )
            y -= 9.6
        y -= 4.0
        tax_line = _fit(f"Tax ID {spec.vendor_tax_id}", _BODY, _SMALL_SIZE, left_room)
        self._text(canvas, self.left, y, tax_line, _BODY, _SMALL_SIZE, _MUTED)

        title_y = y_top - 17.0
        self._right_text(canvas, self.right, title_y, title, _BOLD, 19.0, _INK)
        meta_y = title_y - 17.0
        label_x = self.right - _META_WIDTH
        for label, value in meta:
            self._text(canvas, label_x, meta_y, label, _BODY, _SMALL_SIZE, _MUTED)
            label_w = pdfmetrics.stringWidth(label, _BODY, _SMALL_SIZE)
            room = _META_WIDTH - label_w - 8.0
            self._right_text(
                canvas,
                self.right,
                meta_y,
                _fit(value, _BOLD, _SMALL_SIZE, room),
                _BOLD,
                _SMALL_SIZE,
                _INK,
            )
            meta_y -= 11.0

        billto_y = y_top - block_h - 13.0
        self._text(canvas, self.left, billto_y, billto_label, _BOLD, 7.0, _MUTED)
        billto_y -= 12.0
        full_room = self.right - self.left
        for line in spec.bill_to_lines:
            self._text(
                canvas,
                self.left,
                billto_y,
                _fit(line, _BODY, _BODY_SIZE, full_room),
                _BODY,
                _BODY_SIZE,
                _INK,
            )
            billto_y -= 10.2
        return total_h

    def _running_header(
        self, y_top: float, canvas: Canvas | None, page_no: int, total_pages: int
    ) -> float:
        height = 26.0
        if canvas is None:
            return height
        kind = "Credit note" if self.credit else "Invoice"
        left = f"{self.spec.vendor_name}  ·  {kind} {self.spec.invoice_number}"
        right = f"continued — page {page_no} of {total_pages}"
        room = (self.right - self.left) - pdfmetrics.stringWidth(right, _ITALIC, 8.0) - 14.0
        left = _fit(left, _BOLD, 8.0, room)
        self._text(canvas, self.left, y_top - 10.0, left, _BOLD, 8.0, _INK)
        self._right_text(canvas, self.right, y_top - 10.0, right, _ITALIC, 8.0, _MUTED)
        self._rule(canvas, y_top - 16.0, 0.7, _RULE)
        return height

    def _brought_forward(
        self, y_top: float, canvas: Canvas | None, page_no: int, running: Decimal
    ) -> float:
        if canvas is None:
            return _BROUGHT_H
        baseline = y_top - 12.0
        canvas.setFillColor(_BAND)
        canvas.rect(self.left, baseline - 4.5, self.right - self.left, 15.0, stroke=0, fill=1)
        label = self.brought_label.format(prev=page_no - 1)
        self._cell(canvas, "description", baseline, label, _ITALIC, _BODY_SIZE, _INK)
        self._cell(
            canvas, "amount", baseline, _amount(running, self.currency), _BOLD, _BODY_SIZE, _INK
        )
        return _BROUGHT_H

    def _column_header(self, y_top: float, canvas: Canvas | None) -> float:
        if canvas is None:
            return _COLHDR_H
        self._rule(canvas, y_top, 0.8, _RULE)
        baseline = y_top - 11.0
        for column in self.columns:
            self._cell(canvas, column.key, baseline, column.label, _BOLD, _SMALL_SIZE, _INK)
        self._rule(canvas, y_top - _COLHDR_H + 2.0, 0.5, _RULE)
        return _COLHDR_H

    def _draw_slice(self, canvas: Canvas, row: _Row, chunk: _Slice, y_top: float) -> None:
        baseline = y_top - _BODY_LEADING + 2.0
        item = row.item
        for offset, line in enumerate(row.desc_lines[chunk.start : chunk.end]):
            self._cell(
                canvas,
                "description",
                baseline - offset * _BODY_LEADING,
                line,
                _BODY,
                _BODY_SIZE,
                _INK,
            )
        if chunk.first:
            if "sku" in self.col_x:
                _, width = self.col_x["sku"]
                code = _fit(item.sku or "—", _MONO, 7.0, width - 2 * _CELL_PAD)
                self._cell(canvas, "sku", baseline, code, _MONO, 7.0, _MUTED)
            self._cell(canvas, "uom", baseline, item.unit_of_measure, _BODY, _SMALL_SIZE, _MUTED)
            quantity = _quantity(item.quantity, self.currency)
            self._cell(canvas, "qty", baseline, quantity, _BODY, _BODY_SIZE, _INK)
            self._cell(
                canvas,
                "unit",
                baseline,
                _amount(item.unit_price, self.currency),
                _BODY,
                _BODY_SIZE,
                _INK,
            )
            self._cell(
                canvas,
                "amount",
                baseline,
                _amount(item.line_total, self.currency),
                _BODY,
                _BODY_SIZE,
                _INK,
            )
        foot = y_top - chunk.height()
        canvas.setStrokeColor(_HAIRLINE)
        canvas.setLineWidth(0.4)
        canvas.line(self.left, foot, self.right, foot)

    def _continued_foot(
        self, y_top: float, canvas: Canvas, page_no: int, total_pages: int, running: Decimal
    ) -> None:
        self._rule(canvas, y_top, 0.6, _RULE)
        baseline = y_top - 11.0
        label = self.carried_label.format(next=page_no + 1)
        self._cell(canvas, "description", baseline, label, _ITALIC, _BODY_SIZE, _INK)
        self._cell(
            canvas, "amount", baseline, _amount(running, self.currency), _BOLD, _BODY_SIZE, _INK
        )
        phrase = self.continued_phrase.format(next=page_no + 1, total=total_pages)
        self._right_text(canvas, self.right, baseline - 12.0, phrase, _BOLD, _SMALL_SIZE, _MUTED)

    def _closing_block(self, y_top: float, canvas: Canvas | None) -> float:
        spec = self.spec
        totals_x = self.right
        label_x = self.right - _TOTALS_WIDTH

        rows: list[tuple[str, str]] = [
            ("Subtotal", _amount(spec.subtotal, self.currency)),
            (f"Tax @ {_percent(spec.tax_rate)}", _amount(spec.tax_amount, self.currency)),
        ]
        grand_label = "TOTAL CREDIT" if self.credit else "TOTAL DUE"
        grand_value = f"{self.currency} {_amount(spec.total, self.currency)}"

        settle_lines: list[tuple[str, str]] = [
            ("Terms", spec.payment_terms),
            ("Due", _date(spec.due_date)),
            ("IBAN", f"•••• {spec.bank_iban_last4}"),
            ("Routing / sort code", f"•••• {spec.bank_routing_last4}"),
        ]
        if self.credit:
            settle_note = (
                f"No payment is due. This credit is applied against invoice "
                f"{self.original_reference}."
            )
        else:
            settle_note = "Remit by the due date quoting the document number above."
        note_lines = _wrap(settle_note, _BODY, _SMALL_SIZE, (self.right - self.left) * 0.48)

        totals_h = 10.0 + len(rows) * 12.5 + 8.0 + 20.0
        settle_h = 10.0 + 12.0 + len(settle_lines) * 10.5 + 4.0 + len(note_lines) * 9.2
        block_h = max(totals_h, settle_h)

        extra = 0.0
        spec_notes = spec.notes
        note_body: tuple[str, ...] = ()
        if spec_notes:
            note_body = _wrap(spec_notes, _BODY, _SMALL_SIZE, self.right - self.left)
            extra += 8.0 + 10.0 + len(note_body) * 9.4
        extra += 8.0 + 9.4  # boilerplate line

        if canvas is None:
            return 10.0 + block_h + extra

        y = y_top - 10.0
        # Totals, right hand side. This block exists on the final page only.
        row_y = y - 10.0
        for label, value in rows:
            self._text(canvas, label_x, row_y, label, _BODY, _BODY_SIZE, _MUTED)
            self._right_text(canvas, totals_x, row_y, value, _BODY, _BODY_SIZE, _INK)
            row_y -= 12.5
        canvas.setStrokeColor(_RULE)
        canvas.setLineWidth(0.8)
        canvas.line(label_x, row_y + 4.0, totals_x, row_y + 4.0)
        row_y -= 12.0
        grand_room = _TOTALS_WIDTH - pdfmetrics.stringWidth(grand_value, _BOLD, 10.5) - 10.0
        self._text(
            canvas, label_x, row_y, _fit(grand_label, _BOLD, 10.5, grand_room), _BOLD, 10.5, _INK
        )
        self._right_text(canvas, totals_x, row_y, grand_value, _BOLD, 10.5, _INK)
        canvas.setStrokeColor(_INK)
        canvas.setLineWidth(0.9)
        canvas.line(label_x, row_y - 6.0, totals_x, row_y - 6.0)

        # Settlement / remittance, left hand side.
        settle_y = y - 10.0
        heading = "CREDIT SETTLEMENT" if self.credit else "PAYMENT / REMITTANCE"
        self._text(canvas, self.left, settle_y, heading, _BOLD, 7.0, _MUTED)
        settle_y -= 12.0
        settle_room = (label_x - 12.0) - (self.left + 96.0)
        for label, value in settle_lines:
            self._text(canvas, self.left, settle_y, label, _BODY, _SMALL_SIZE, _MUTED)
            self._text(
                canvas,
                self.left + 96.0,
                settle_y,
                _fit(value, _BODY, _SMALL_SIZE, settle_room),
                _BODY,
                _SMALL_SIZE,
                _INK,
            )
            settle_y -= 10.5
        settle_y -= 4.0
        for line in note_lines:
            self._text(canvas, self.left, settle_y, line, _BODY, _SMALL_SIZE, _INK)
            settle_y -= 9.2

        tail_y = y_top - 10.0 - block_h - 8.0
        if note_body:
            self._text(canvas, self.left, tail_y, "NOTES", _BOLD, 7.0, _MUTED)
            tail_y -= 10.0
            for line in note_body:
                self._text(canvas, self.left, tail_y, line, _BODY, _SMALL_SIZE, _INK)
                tail_y -= 9.4
        tail_y -= 8.0
        self._text(canvas, self.left, tail_y, self.boilerplate, _ITALIC, _SMALL_SIZE, _MUTED)
        return 10.0 + block_h + extra

    def _footer(self, canvas: Canvas, page_no: int, total_pages: int) -> None:
        y = self.bottom + 11.0
        self._rule(canvas, y + 8.0, 0.5, _HAIRLINE)
        kind = "Credit note" if self.credit else "Invoice"
        centre = f"Doc control {self.doc_control}"
        centre_x = (self.left + self.right) / 2.0
        centre_w = pdfmetrics.stringWidth(centre, _BODY, _SMALL_SIZE)
        left = f"{kind} {self.spec.invoice_number}  ·  {self.spec.vendor_name}"
        left_room = centre_x - centre_w / 2.0 - self.left - 10.0
        self._text(
            canvas,
            self.left,
            y,
            _fit(left, _BODY, _SMALL_SIZE, left_room),
            _BODY,
            _SMALL_SIZE,
            _MUTED,
        )
        canvas.setFont(_BODY, _SMALL_SIZE)
        canvas.setFillColor(_HAIRLINE)
        canvas.drawCentredString(centre_x, y, centre)
        self._right_text(
            canvas, self.right, y, f"Page {page_no} of {total_pages}", _BOLD, _SMALL_SIZE, _MUTED
        )

    # -- pagination -------------------------------------------------------
    def _build_rows(self) -> tuple[_Row, ...]:
        width = self._desc_width()
        return tuple(
            _Row(item=item, desc_lines=_wrap(item.description, _BODY, _BODY_SIZE, width))
            for item in self.spec.line_items
        )

    def _body_top(self, page_no: int) -> float:
        if page_no == 1:
            return self.top - self._masthead(self.top, None)
        return self.top - self._running_header(self.top, None, page_no, page_no)

    def _capacity(self, page_no: int) -> float:
        """Row height available on ``page_no``, reserving the continuation foot."""
        top = self._body_top(page_no) - _COLHDR_H
        if page_no > 1:
            top -= _BROUGHT_H
        floor = self.bottom + _FOOTER_H + _CONT_FOOT_H
        return top - floor

    def _paginate(self, rows: tuple[_Row, ...]) -> tuple[tuple[_Slice, ...], ...]:
        pages: list[list[_Slice]] = []
        current: list[_Slice] = []
        used = 0.0
        capacity = self._capacity(1)
        for index, row in enumerate(rows):
            start = 0
            count = len(row.desc_lines)
            while start < count:
                whole = _Slice(index, start, count)
                if whole.height() <= capacity - used:
                    current.append(whole)
                    used += whole.height()
                    break
                if current:
                    pages.append(current)
                    current = []
                    used = 0.0
                    capacity = self._capacity(len(pages) + 1)
                    continue
                # A fresh page and the item still does not fit: split it.
                fits = max(1, int((capacity - _ROW_PAD) // _BODY_LEADING))
                part = _Slice(index, start, min(count, start + fits))
                current.append(part)
                used += part.height()
                start = part.end
        pages.append(current)

        closing = self._closing_block(0.0, None)
        while len(pages[-1]) > 1:
            used = sum(chunk.height() for chunk in pages[-1])
            final_capacity = self._capacity(len(pages)) + _CONT_FOOT_H
            if final_capacity - used >= closing:
                break
            pages.append([pages[-1].pop()])
        return tuple(tuple(page) for page in pages)

    # -- page assembly ----------------------------------------------------
    def _draw_page(
        self,
        page_no: int,
        total_pages: int,
        chunks: tuple[_Slice, ...],
        rows: tuple[_Row, ...],
        running_before: Decimal,
    ) -> Decimal:
        canvas = self.canvas
        y = self.top
        if page_no == 1:
            y -= self._masthead(y, canvas)
        else:
            y -= self._running_header(y, canvas, page_no, total_pages)
            y -= self._brought_forward(y, canvas, page_no, running_before)
        y -= self._column_header(y, canvas)

        running = running_before
        for chunk in chunks:
            row = rows[chunk.row]
            self._draw_slice(canvas, row, chunk, y)
            y -= chunk.height()
            if chunk.first:
                running += row.item.line_total

        if page_no < total_pages:
            foot_top = self.bottom + _FOOTER_H + _CONT_FOOT_H
            self._continued_foot(foot_top, canvas, page_no, total_pages, running)
        else:
            self._closing_block(y - 6.0, canvas)
        self._footer(canvas, page_no, total_pages)
        return running

    def run(self) -> None:
        rows = self._build_rows()
        pages = self._paginate(rows)
        total_pages = len(pages)
        running = Decimal(0)
        for page_no, chunks in enumerate(pages, start=1):
            running = self._draw_page(page_no, total_pages, chunks, rows, running)
            self.canvas.showPage()
        self.canvas.save()


def render(spec: InvoiceSpec, path: Path) -> None:
    """Render ``spec`` as a multi-page invoice or credit note at ``path``.

    The spec is read only. Which of the two shapes is produced is decided by
    the spec itself: ``spec.is_credit_note`` selects the credit-note masthead
    and bracketed money, and the length of ``spec.line_items`` decides whether
    the document runs to more than one page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _Renderer(spec, path).run()
