"""Degraded scanned/faxed supplier invoice — the "scanned thermal" layout family.

Modelled on the output of a small supplier's legacy billing system: a 9-pin
dot-matrix or thermal receipt printer driving fixed-pitch Courier at 10 CPI,
with columns laid out in character cells rather than on a vector grid. The
sheet is then fed through a flatbed or an MFP fax bridge, so what the AP
mailbox actually receives is a low-contrast, off-white, speckled raster of a
page that was never square on the glass.

A large share of real accounts-payable volume looks exactly like this, and it
is the shape that separates an extraction pipeline from a demo. Every
degradation here is applied *inside* the PDF — there is no post-processing
step, no image round-trip, and no external binary.

Extraction failure modes this family exercises
----------------------------------------------
Skew
    The whole content block is rotated 0.4-2.0 degrees about the page centre,
    so no text baseline is axis-aligned. Layout engines that assume horizontal
    rows, and heuristics that group cells by shared y, both degrade here.
Ambiguous monospace glyphs
    Courier does not slash its zero and gives ``1``, ``l`` and ``I`` nearly
    identical stems. At the reduced ink contrast used here, any SKU carrying
    ``0``/``O`` or ``1``/``l``/``I`` adjacency becomes genuinely confusable
    once the page is rasterised. The scan-batch stamp along the top edge is
    built entirely from those five glyphs, which also plants a plausible-
    looking identifier that is *not* the invoice number.
Column drift
    The amount and unit-price columns walk left and right down the page under
    a bounded random walk, the way paper skips on a tractor feed. Extractors
    that lock a column to a fixed x, or that infer columns from the first few
    rows, mis-bin values further down.
Obscured total
    A dense speckle cluster is laid over the left portion of the grand total,
    leaving the trailing digits legible. This is the specific failure a
    three-way match must survive: a total that reads as *almost* right.
Absent vector structure
    Horizontal rules are runs of ``-`` and ``=`` characters with per-run
    dropout and baseline jitter, not lines. Table detection that keys on ruled
    borders finds nothing to key on.

Determinism
-----------
Every stochastic choice — skew angle and sign, ink level per line, character
jitter, rule dropout, column drift, and all speckle — is drawn from a single
``random.Random(spec.seed)``. reportlab's creation timestamp and document id
are suppressed via ``invariant=1``, and all document metadata is derived from
the spec, so the same ``InvoiceSpec`` renders byte-identical output.
"""

from __future__ import annotations

import random
from decimal import Decimal
from math import cos, radians, sin
from pathlib import Path
from typing import Final

from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from seeds.invoice_spec import InvoiceSpec, LineItem

LAYOUT_NAME: Final[str] = "scanned_thermal"

# --- sheet geometry ---------------------------------------------------------
# US Letter. Character-cell layout: Courier advances at exactly 0.6 em, so a
# column is an integer number of cells and the whole page can be reasoned about
# in columns the way the originating print driver would have.
_PAGE_W: Final[float] = 612.0
_PAGE_H: Final[float] = 792.0
_MARGIN_L: Final[float] = 52.0
_TOP_Y: Final[float] = 736.0
_BOTTOM_Y: Final[float] = 76.0

_MONO: Final[str] = "Courier"
_MONO_BOLD: Final[str] = "Courier-Bold"
_BODY: Final[float] = 8.4
_CHAR_W: Final[float] = _BODY * 0.6
_LINE_H: Final[float] = 10.4
_COLS: Final[int] = 100

# --- character-cell columns -------------------------------------------------
_QTY_R: Final[int] = 7  # right edge
_UM_C: Final[int] = 9
_SKU_C: Final[int] = 13
_SKU_W: Final[int] = 13
_DESC_C: Final[int] = 27
_DESC_W: Final[int] = 37
_UNIT_R: Final[int] = 82  # right edge
_AMT_R: Final[int] = 99  # right edge
_LABEL_R: Final[int] = 82  # totals labels, right edge
_WIDE_W: Final[int] = 88  # wrap width for free-text blocks

# Locale-independent month names. ``strftime("%b")`` is locale-sensitive and
# would make output machine-dependent, which is disqualifying for a golden set.
# fmt: off
_MONTHS: Final[tuple[str, ...]] = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
)
# fmt: on

#: The five glyphs that a low-contrast fixed-pitch raster collapses onto each
#: other. The scan-batch stamp is built from these and nothing else.
_CONFUSABLE: Final[str] = "0O1lI"


# ---------------------------------------------------------------------------
# formatting — Decimal in, str out, no float anywhere on the path
# ---------------------------------------------------------------------------
def _money(value: Decimal) -> str:
    """Group-separated fixed-point. Decimal formats natively; never via float."""
    return f"{value:,.2f}"


def _quantity(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.to_integral_value())
    text = f"{value:f}".rstrip("0").rstrip(".")
    return text or "0"


def _percent(rate: Decimal) -> str:
    """``tax_rate`` is read as a fraction: 0.0825 renders as 8.25%."""
    text = f"{rate * Decimal(100):.3f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _legacy_date(year: int, month: int, day: int) -> str:
    """DD-MON-YYYY, the format legacy AP systems actually emit.

    Deliberately not ISO — it forces date normalisation — but unlike a bare
    numeric format it cannot be misread as day-first versus month-first.
    """
    return f"{day:02d}-{_MONTHS[month - 1]}-{year:04d}"


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for raw in words:
        word = raw
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# the sheet
# ---------------------------------------------------------------------------
class _Sheet:
    """One physical scan: paper transform, ink model, and page furniture."""

    def __init__(
        self,
        pdf: Canvas,
        rng: random.Random,
        angle_deg: float,
        spec: InvoiceSpec,
    ) -> None:
        self.pdf = pdf
        self.rng = rng
        self.angle_deg = angle_deg
        self.angle_rad = radians(angle_deg)
        self.pivot_x = _PAGE_W / 2.0
        self.pivot_y = _PAGE_H / 2.0
        self.y = _TOP_Y
        self.page_no = 1
        self.drift = 0.0
        self.continuation = f"{spec.vendor_name} / INV {spec.invoice_number}"
        self.image_seq = rng.randint(1000, 9999)
        self.batch = "".join(rng.choice(_CONFUSABLE) for _ in range(6))
        self.total_box: tuple[float, float, float, float] | None = None

    # -- coordinate helpers -------------------------------------------------
    def col_x(self, col: int) -> float:
        return _MARGIN_L + col * _CHAR_W

    def to_page(self, x: float, y: float) -> tuple[float, float]:
        """Content-space point to page-space, matching the canvas transform."""
        dx = x - self.pivot_x
        dy = y - self.pivot_y
        cos_a = cos(self.angle_rad)
        sin_a = sin(self.angle_rad)
        return (
            self.pivot_x + dx * cos_a - dy * sin_a,
            self.pivot_y + dx * sin_a + dy * cos_a,
        )

    # -- ink ----------------------------------------------------------------
    def _ink(self) -> float:
        """Grey level for one line. Never pure black; occasionally starved."""
        if self.rng.random() < 0.09:
            return 0.44 + self.rng.random() * 0.13
        return 0.19 + self.rng.random() * 0.14

    def put(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float = _BODY,
        bold: bool = False,
        gray: float | None = None,
        right: bool = False,
    ) -> None:
        if not text:
            return
        pdf = self.pdf
        pdf.setFont(_MONO_BOLD if bold else _MONO, size)
        pdf.setFillGray(self._ink() if gray is None else gray)
        dx = self.rng.uniform(-0.55, 0.55)
        dy = self.rng.uniform(-0.42, 0.42)
        if right:
            pdf.drawRightString(x + dx, y + dy, text)
        else:
            pdf.drawString(x + dx, y + dy, text)

    # -- line flow ----------------------------------------------------------
    def feed(self, lines: float = 1.0) -> None:
        self.y -= _LINE_H * lines

    def need(self, lines: float) -> None:
        if self.y - _LINE_H * lines < _BOTTOM_Y:
            self.break_page()

    def row(self, col: int, text: str, *, bold: bool = False, size: float = _BODY) -> None:
        self.need(1)
        self.put(self.col_x(col), self.y, text, bold=bold, size=size)
        self.feed()

    def rule(self, glyph: str = "-") -> None:
        """A rule struck from characters, with pin dropout and baseline wobble.

        Real dot-matrix separators are printed glyphs, not vectors, and a scan
        of one is neither straight nor continuous.
        """
        self.need(1)
        pdf = self.pdf
        pdf.setFont(_MONO, _BODY)
        col = 0
        while col < _COLS:
            run = min(self.rng.randint(5, 13), _COLS - col)
            if self.rng.random() >= 0.07:  # else: the head skipped this run
                pdf.setFillGray(0.28 + self.rng.random() * 0.27)
                pdf.drawString(
                    self.col_x(col) + self.rng.uniform(-0.5, 0.5),
                    self.y + self.rng.uniform(-0.5, 0.5),
                    glyph * run,
                )
            col += run
        self.feed()

    def step_drift(self) -> float:
        """Bounded random walk of the money columns, in points."""
        self.drift = max(-4.5, min(4.5, self.drift + self.rng.uniform(-0.95, 0.95)))
        return self.drift

    # -- page furniture -----------------------------------------------------
    def begin_page(self) -> None:
        self._wash()
        self._scan_stamp()
        pdf = self.pdf
        pdf.saveState()
        pdf.translate(self.pivot_x, self.pivot_y)
        pdf.rotate(self.angle_deg)
        pdf.translate(-self.pivot_x, -self.pivot_y)
        self.y = _TOP_Y

    def end_page(self) -> None:
        self.pdf.restoreState()
        self._streaks()
        self._speckle_field()
        self._edge_grime()
        if self.total_box is not None:
            self._speckle_cluster(self.total_box)
            self.total_box = None
        self.pdf.showPage()

    def break_page(self) -> None:
        self.put(
            self.col_x(0),
            self.y,
            f"... CONTINUED ON PAGE {self.page_no + 1}",
            gray=0.42,
        )
        self.end_page()
        self.page_no += 1
        self.image_seq += 1
        self.begin_page()
        self.put(self.col_x(0), self.y, self.continuation)
        self.put(self.col_x(_AMT_R), self.y, f"PAGE {self.page_no}", right=True)
        self.feed()
        self.rule("-")

    # -- degradation --------------------------------------------------------
    def _wash(self) -> None:
        """Off-white ground with uneven blotching, as an MFP scan of bond paper.

        Many low-contrast overlapping patches rather than a few strong ones:
        individually invisible, collectively an uneven exposure. A handful of
        high-contrast blocks would read as compositing, which is the one thing
        a fixture must never look like.
        """
        pdf = self.pdf
        pdf.setFillGray(0.905)
        pdf.rect(0, 0, _PAGE_W, _PAGE_H, stroke=0, fill=1)
        for _ in range(110):
            pdf.setFillGray(0.897 + self.rng.random() * 0.016)
            pdf.rect(
                self.rng.uniform(-80.0, _PAGE_W),
                self.rng.uniform(-80.0, _PAGE_H),
                self.rng.uniform(70.0, 300.0),
                self.rng.uniform(60.0, 280.0),
                stroke=0,
                fill=1,
            )
        # Platen vignette: nested frames darkening toward the sheet edge, which
        # is what a lid that does not sit flat does to a scan.
        for step in range(12):
            inset = step * 3.2
            pdf.setFillGray(0.858 + step * 0.0042)
            band = 3.4
            pdf.rect(inset, inset, _PAGE_W - 2 * inset, band, stroke=0, fill=1)
            pdf.rect(inset, _PAGE_H - inset - band, _PAGE_W - 2 * inset, band, stroke=0, fill=1)
            pdf.rect(inset, inset, band, _PAGE_H - 2 * inset, stroke=0, fill=1)
            pdf.rect(_PAGE_W - inset - band, inset, band, _PAGE_H - 2 * inset, stroke=0, fill=1)

    def _scan_stamp(self) -> None:
        """Imaging header burnt in by the capture device, in *page* space.

        It sits square to the sheet while the invoice itself is skewed, which
        is exactly what a stamp applied after digitisation looks like. Its
        batch id is drawn only from 0/O/1/l/I, so it is both a glyph-ambiguity
        probe and a decoy identifier competing with the real invoice number.
        """
        pdf = self.pdf
        pdf.setFont(_MONO, 6.2)
        pdf.setFillGray(0.46 + self.rng.random() * 0.08)
        pdf.drawString(
            _MARGIN_L - 8.0,
            _PAGE_H - 30.0,
            f"SCAN BATCH {self.batch}   IMG {self.image_seq:05d}   PG {self.page_no:02d}",
        )

    def _streaks(self) -> None:
        """Feed-roller marks: faint, thin, and not always the full sheet."""
        for _ in range(self.rng.randint(1, 3)):
            height = _PAGE_H * self.rng.uniform(0.45, 1.0)
            self.pdf.setFillGray(0.80 + self.rng.random() * 0.10)
            self.pdf.rect(
                self.rng.uniform(20.0, _PAGE_W - 20.0),
                self.rng.uniform(0.0, _PAGE_H - height),
                self.rng.uniform(0.35, 0.95),
                height,
                stroke=0,
                fill=1,
            )

    def _speckle_field(self) -> None:
        pdf = self.pdf
        for _ in range(self.rng.randint(950, 1450)):
            pdf.setFillGray(0.34 + self.rng.random() * 0.44)
            size = self.rng.uniform(0.12, 0.62)
            pdf.rect(
                self.rng.uniform(0.0, _PAGE_W),
                self.rng.uniform(0.0, _PAGE_H),
                size,
                size,
                stroke=0,
                fill=1,
            )

    def _edge_grime(self) -> None:
        """Platen-edge dirt: dense at one margin, thinning inward."""
        pdf = self.pdf
        near_left = self.rng.random() < 0.5
        for _ in range(self.rng.randint(200, 320)):
            depth = self.rng.random() ** 2.4 * 36.0
            x = depth if near_left else _PAGE_W - depth
            pdf.setFillGray(0.30 + self.rng.random() * 0.40)
            size = self.rng.uniform(0.15, 0.85)
            pdf.rect(x, self.rng.uniform(0.0, _PAGE_H), size, size, stroke=0, fill=1)
        for _ in range(self.rng.randint(120, 200)):
            depth = self.rng.random() ** 2.2 * 30.0
            pdf.setFillGray(0.34 + self.rng.random() * 0.38)
            size = self.rng.uniform(0.15, 0.8)
            pdf.rect(self.rng.uniform(0.0, _PAGE_W), depth, size, size, stroke=0, fill=1)

    def _speckle_cluster(self, box: tuple[float, float, float, float]) -> None:
        """Toner debris over the *leading* digits of the grand total.

        Centred at a third of the way across the figure with a tight spread,
        so the trailing digits survive. A total that is unreadable is a thrown
        exception; a total that reads as almost right is the case a matching
        engine has to catch.
        """
        x0, y0, x1, y1 = box
        cx, cy = self.to_page(x0 + (x1 - x0) * 0.30, (y0 + y1) / 2.0)
        spread_x = max(3.0, (x1 - x0) * 0.27)
        spread_y = max(2.0, (y1 - y0) * 0.85)
        pdf = self.pdf
        for _ in range(self.rng.randint(65, 105)):
            pdf.setFillGray(0.20 + self.rng.random() * 0.36)
            pdf.circle(
                cx + self.rng.gauss(0.0, spread_x),
                cy + self.rng.gauss(0.0, spread_y),
                self.rng.uniform(0.35, 1.75),
                stroke=0,
                fill=1,
            )
        for _ in range(self.rng.randint(2, 4)):
            pdf.setFillGray(0.30 + self.rng.random() * 0.22)
            pdf.circle(
                cx + self.rng.gauss(0.0, spread_x * 0.75),
                cy + self.rng.gauss(0.0, spread_y * 0.75),
                self.rng.uniform(1.6, 2.7),
                stroke=0,
                fill=1,
            )


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------
def _spaced(text: str) -> str:
    return " ".join(text)


def _masthead(sheet: _Sheet, spec: InvoiceSpec) -> None:
    sheet.need(4 + len(spec.vendor_address_lines))
    sheet.put(sheet.col_x(0), sheet.y, spec.vendor_name, size=12.4, bold=True)
    sheet.feed(1.6)
    for line in spec.vendor_address_lines:
        sheet.row(0, line)
    sheet.row(0, f"TAX ID {spec.vendor_tax_id}")
    sheet.feed(0.4)
    sheet.rule("=")


def _document_block(sheet: _Sheet, spec: InvoiceSpec) -> None:
    title = "C R E D I T   N O T E" if spec.is_credit_note else _spaced("INVOICE")
    sheet.need(7)
    sheet.put(sheet.col_x(0), sheet.y, title, size=10.2, bold=True)
    sheet.feed(1.5)

    issue = _legacy_date(spec.issue_date.year, spec.issue_date.month, spec.issue_date.day)
    due = _legacy_date(spec.due_date.year, spec.due_date.month, spec.due_date.day)

    left: list[tuple[str, str]] = [("INVOICE NO", spec.invoice_number)]
    if spec.po_number is not None:
        left.append(("PO NUMBER", spec.po_number))
    left.append(("CURRENCY", spec.currency))
    right: list[tuple[str, str]] = [("ISSUE DATE", issue), ("DUE DATE", due)]

    for index in range(max(len(left), len(right))):
        sheet.need(1)
        if index < len(left):
            label, value = left[index]
            sheet.put(sheet.col_x(0), sheet.y, f"{label:<11}: {value}")
        if index < len(right):
            label, value = right[index]
            sheet.put(sheet.col_x(52), sheet.y, f"{label:<11}: {value}")
        sheet.feed()

    sheet.feed(0.3)
    sheet.rule("-")
    sheet.row(0, "BILL TO", bold=True)
    for line in spec.bill_to_lines:
        sheet.row(2, line)
    sheet.feed(0.3)
    sheet.rule("=")


def _item_header(sheet: _Sheet) -> None:
    sheet.need(2)
    sheet.put(sheet.col_x(_QTY_R), sheet.y, "QTY", bold=True, right=True)
    sheet.put(sheet.col_x(_UM_C), sheet.y, "UM", bold=True)
    sheet.put(sheet.col_x(_SKU_C), sheet.y, "PART/SKU", bold=True)
    sheet.put(sheet.col_x(_DESC_C), sheet.y, "DESCRIPTION", bold=True)
    sheet.put(sheet.col_x(_UNIT_R), sheet.y, "UNIT PRICE", bold=True, right=True)
    sheet.put(sheet.col_x(_AMT_R), sheet.y, "AMOUNT", bold=True, right=True)
    sheet.feed()
    sheet.rule("-")


def _item_rows(sheet: _Sheet, item: LineItem) -> None:
    body = _wrap(item.description, _DESC_W) or [""]
    sku = item.sku or ""
    sku_overflows = len(sku) > _SKU_W
    sheet.need(len(body) + (1 if sku_overflows else 0) + 1)

    drift = sheet.step_drift()
    sheet.put(sheet.col_x(_QTY_R), sheet.y, _quantity(item.quantity), right=True)
    sheet.put(sheet.col_x(_UM_C), sheet.y, item.unit_of_measure)
    if sku and not sku_overflows:
        sheet.put(sheet.col_x(_SKU_C), sheet.y, sku)
    sheet.put(sheet.col_x(_DESC_C), sheet.y, body[0])
    sheet.put(sheet.col_x(_UNIT_R) + drift * 0.6, sheet.y, _money(item.unit_price), right=True)
    sheet.put(sheet.col_x(_AMT_R) + drift, sheet.y, _money(item.line_total), right=True)
    sheet.feed()

    for continued in body[1:]:
        sheet.put(sheet.col_x(_DESC_C), sheet.y, continued)
        sheet.feed()
    if sku_overflows:
        # Too wide for its cell, so the driver breaks it onto its own line
        # rather than overprinting the description.
        sheet.put(sheet.col_x(_SKU_C), sheet.y, f"PART {sku}")
        sheet.feed()
    sheet.feed(0.4)


def _totals(sheet: _Sheet, spec: InvoiceSpec) -> None:
    sheet.rule("-")
    sheet.need(6)

    drift = sheet.step_drift()
    sheet.put(sheet.col_x(_LABEL_R), sheet.y, "SUBTOTAL", right=True)
    sheet.put(sheet.col_x(_AMT_R) + drift, sheet.y, _money(spec.subtotal), right=True)
    sheet.feed()

    drift = sheet.step_drift()
    sheet.put(sheet.col_x(_LABEL_R), sheet.y, f"TAX @ {_percent(spec.tax_rate)}", right=True)
    sheet.put(sheet.col_x(_AMT_R) + drift, sheet.y, _money(spec.tax_amount), right=True)
    sheet.feed()

    # The "double rule" over a total on fixed-pitch stationery is struck, not drawn.
    sheet.put(sheet.col_x(_AMT_R) + drift, sheet.y, "=" * 18, right=True, gray=0.36)
    sheet.feed(0.9)

    drift = sheet.step_drift()
    total_size = 9.8
    total_text = _money(spec.total)
    label = "TOTAL DUE" if not spec.is_credit_note else "CREDIT TOTAL"
    sheet.put(sheet.col_x(_LABEL_R), sheet.y, f"{label} {spec.currency}", right=True, bold=True)
    right_x = sheet.col_x(_AMT_R) + drift
    sheet.put(right_x, sheet.y, total_text, right=True, bold=True, size=total_size)
    sheet.total_box = (
        right_x - len(total_text) * total_size * 0.6,
        sheet.y,
        right_x,
        sheet.y + total_size * 0.72,
    )
    sheet.feed(1.2)
    sheet.rule("=")


def _footer(sheet: _Sheet, spec: InvoiceSpec) -> None:
    sheet.row(0, "REMITTANCE", bold=True)
    sheet.row(
        2,
        f"IBAN ....{spec.bank_iban_last4}     ROUTING/ABA ....{spec.bank_routing_last4}",
    )
    sheet.feed(0.3)
    sheet.row(0, "PAYMENT TERMS", bold=True)
    for line in _wrap(spec.payment_terms, _WIDE_W) or ["-"]:
        sheet.row(2, line)

    if spec.notes is not None:
        sheet.feed(0.3)
        sheet.row(0, "NOTES", bold=True)
        for line in _wrap(spec.notes, _WIDE_W):
            sheet.row(2, line)

    sheet.feed(0.4)
    sheet.rule("-")
    sheet.need(1)
    sheet.put(sheet.col_x(0), sheet.y, "*** END OF DOCUMENT ***", gray=0.40)
    sheet.put(sheet.col_x(_AMT_R), sheet.y, f"PAGE {sheet.page_no}", right=True, gray=0.40)
    sheet.feed()


def render(spec: InvoiceSpec, path: Path) -> None:
    """Render ``spec`` as a degraded scan of a fixed-pitch supplier invoice.

    Writes a PDF at ``path``. Deterministic: identical ``spec`` in, identical
    bytes out. Does not mutate ``spec``.
    """
    rng = random.Random(spec.seed)

    # Skew magnitude is held inside the brief's 0.4-2.0 degree band; the sign
    # is free, because paper lands on the glass crooked in both directions.
    magnitude = 0.4 + rng.random() * 1.6
    angle_deg = magnitude if rng.random() < 0.5 else -magnitude

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = Canvas(
        str(path),
        pagesize=(_PAGE_W, _PAGE_H),
        invariant=1,  # suppresses CreationDate/ModDate and randomised /ID
        pageCompression=1,
    )
    # Every metadata field is spec-derived, so nothing ambient leaks into bytes.
    pdf.setTitle(f"{spec.invoice_number}")
    pdf.setAuthor(spec.vendor_name)
    pdf.setSubject(f"{LAYOUT_NAME} fixture")
    pdf.setCreator(f"threshold seeds/{LAYOUT_NAME}")
    pdf.setProducer(f"threshold seeds/{LAYOUT_NAME}")

    sheet = _Sheet(pdf, rng, angle_deg, spec)
    sheet.begin_page()

    _masthead(sheet, spec)
    _document_block(sheet, spec)
    _item_header(sheet)
    for item in spec.line_items:
        _item_rows(sheet, item)
    _totals(sheet, spec)
    _footer(sheet, spec)

    sheet.end_page()
    pdf.save()
