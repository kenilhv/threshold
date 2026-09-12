"""Continental tabular invoice layout — the dense, formal European B2B document.

Modelled on the invoices that German and Dutch industrial suppliers still send:
Mittelstand machine-parts wholesalers, Dutch chemical and fastener distributors,
the kind of A4 sheet produced by SAP or Exact and printed on pre-ruled stationery.
The visual grammar is deliberate — a heavy fully-ruled article table with visible
cell borders, right-aligned numeric columns, the VAT rate stated on a line of its
own, a boxed ``Bankverbindung`` / ``Bankgegevens`` footer, and the vendor's tax
identifier up in the header block where a European buyer's AP clerk expects to
find it rather than buried in the footer.

Extraction failure modes this family exercises on purpose
---------------------------------------------------------

* **European decimal punctuation.** Whenever the currency is EUR every amount is
  rendered ``1.234,56`` — thousands separated by ``.`` and decimals by ``,``. This
  is the single most common silent extraction error on non-US documents: an
  extractor trained on US invoices reads ``1.234,56`` as ``1.234`` and quietly
  reports a EUR 1.23 invoice as approved.
* **``DD.MM.YYYY`` dates.** ``03.04.2026`` is 3 April, not 4 March. Every date on
  the document is ambiguous under a US parser for twelve days of every month.
* **A tax label that is not "Tax" or "VAT".** The line reads ``zzgl. MwSt. 19,00 %``
  or ``Te vermeerderen met BTW 21,00 %``. Keyword-anchored extractors that scan for
  ``VAT``/``Tax`` find nothing and fall back to guessing which number near the
  bottom is the tax — and the rate is embedded in the label, not in its own field.
* **Wrapped line-item descriptions.** Long article descriptions wrap to a second
  and third line *inside* their cell. The extractor has to keep the continuation
  text bound to its own row instead of leaking it into the next article or
  splitting one article into two.
* **The total printed twice.** Once as the closing row of the ruled table
  (``Gesamtbetrag``) and once in a separate double-ruled summary box
  (``Rechnungsbetrag``). An extractor that takes the last currency-looking number
  on the page, or that averages candidates, gets this wrong in a different way
  than one that takes the first.
* **Currency stated in the column header, not beside the figure.** The article
  table's money columns are headed ``Einzelpreis EUR`` and the cells carry bare
  numbers, so the currency has to be carried down from the header.
* **Four-decimal unit prices.** Where a spec's unit price carries sub-cent
  precision — normal for cheap bulk parts — the unit-price column prints four
  decimals while every other money column prints two, on the same page.
* **Two languages over one structure.** The same layout renders as German or Dutch,
  chosen from the vendor's tax-identifier country and falling back to the seed. An
  extractor that has memorised one label set fails on the other while the document
  geometry is unchanged.

Determinism
-----------
Every varying choice (document language, header band tone, rule weights, summary
box treatment) is drawn from a local ``random.Random(spec.seed)``. The canvas is
opened with ``invariant=True`` so reportlab emits a fixed ``/CreationDate`` and a
fixed document ``/ID``, and the producer string is pinned so output does not shift
between reportlab releases. Same spec in, byte-identical PDF out.

The spec *is* the answer key — `evals/scoring.py` reads `total`, `issue_date` and
the rest straight off it — so there is no separate set of expected values to keep
in step. ``seed`` is a control knob and is not printed. Every other field on the spec appears on
the page; a spec whose ``total`` is negative renders as a ``GUTSCHRIFT`` /
``CREDITFACTUUR`` with the settlement wording a credit note actually carries.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final, Literal

from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from seeds.invoice_spec import InvoiceSpec, LineItem

LAYOUT_NAME: Final = "continental_tabular"

__all__ = ["LAYOUT_NAME", "render"]

# --------------------------------------------------------------------------- #
# Page geometry. A4 because this family is European; the column widths are the
# proportions a machine-parts article table actually uses — a wide description
# column and tight, right-aligned money columns.
# --------------------------------------------------------------------------- #

_PAGE_W: Final[float] = float(A4[0])
_PAGE_H: Final[float] = float(A4[1])

_MARGIN_L: Final[float] = 54.0
_MARGIN_R: Final[float] = 54.0
_MARGIN_T: Final[float] = 46.0
_MARGIN_B: Final[float] = 56.0

_CONTENT_W: Final[float] = _PAGE_W - _MARGIN_L - _MARGIN_R

_FONT: Final[str] = "Helvetica"
_FONT_B: Final[str] = "Helvetica-Bold"
_FONT_O: Final[str] = "Helvetica-Oblique"

_BODY: Final[float] = 8.2
_SMALL: Final[float] = 6.9
_LEAD: Final[float] = 10.2

_CELL_PAD_X: Final[float] = 4.0
_CELL_PAD_Y: Final[float] = 3.4

#: Pos. | Artikel-Nr. | Bezeichnung | Menge | Einheit | Einzelpreis | Betrag
_COL_W: Final[tuple[float, float, float, float, float, float, float]] = (
    26.0,
    74.0,
    173.276,
    46.0,
    36.0,
    64.0,
    64.0,
)

_ALIGN: Final[tuple[str, ...]] = ("l", "l", "l", "r", "c", "r", "r")

_GREY_RULE: Final[float] = 0.35
_INK: Final[float] = 0.0

#: Fixed heights of the closing blocks, so pagination can reserve room for them
#: before it starts drawing the tail of the document.
_TOTALS_ROW_H: Final[float] = 16.0
_TOTALS_TOTAL_ROW_H: Final[float] = 19.0
_TOTALS_H: Final[float] = 2 * _TOTALS_ROW_H + _TOTALS_TOTAL_ROW_H
_SUMMARY_H: Final[float] = 44.0
_SUMMARY_W: Final[float] = 236.0
_BANK_BOX_H: Final[float] = 50.0


# --------------------------------------------------------------------------- #
# Localisation. Two continental dialects of the same document.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Labels:
    """Every visible string that changes with the document's language."""

    lang: Literal["de", "nl"]

    title_invoice: str
    title_credit: str

    tax_id: str
    bill_to: str

    invoice_number: str
    issue_date: str
    due_date: str
    po_number: str
    currency: str

    col_pos: str
    col_sku: str
    col_description: str
    col_quantity: str
    col_unit: str
    col_unit_price: str
    col_amount: str

    subtotal: str
    tax_line: str
    table_total: str

    summary_total: str
    summary_credit: str
    payable_by: str
    settle_by: str

    payment_terms: str
    terms_sentence: str
    terms_sentence_credit: str

    bank_box: str
    account_holder: str
    iban: str
    routing: str
    # IBAN country prefix and total length, so the masked IBAN in the footer
    # has the right shape for the jurisdiction rather than a generic one.
    iban_country: str
    iban_length: int

    notes: str
    page: str
    continued: str


_DE: Final = _Labels(
    lang="de",
    title_invoice="RECHNUNG",
    title_credit="GUTSCHRIFT",
    tax_id="USt-IdNr.",
    bill_to="Rechnungsempfänger",
    invoice_number="Rechnungsnummer",
    issue_date="Rechnungsdatum",
    due_date="Fälligkeitsdatum",
    po_number="Bestellnummer",
    currency="Währung",
    col_pos="Pos.",
    col_sku="Artikel-Nr.",
    col_description="Bezeichnung",
    col_quantity="Menge",
    col_unit="Einheit",
    col_unit_price="Einzelpreis",
    col_amount="Betrag",
    subtotal="Zwischensumme netto",
    tax_line="zzgl. MwSt.",
    table_total="Gesamtbetrag",
    summary_total="Rechnungsbetrag",
    summary_credit="Gutschriftsbetrag",
    payable_by="Zahlbar bis",
    settle_by="Verrechnung zum",
    payment_terms="Zahlungsbedingungen",
    terms_sentence=(
        "Der Rechnungsbetrag ist ohne Abzug bis zum {due} auf das unten genannte "
        "Konto zu überweisen. Bitte geben Sie bei der Zahlung die Rechnungsnummer "
        "{number} als Verwendungszweck an."
    ),
    terms_sentence_credit=(
        "Der Gutschriftsbetrag wird zum {due} mit offenen Forderungen verrechnet. "
        "Eine Auszahlung erfolgt nur auf schriftliche Anforderung unter Angabe der "
        "Gutschriftsnummer {number}."
    ),
    bank_box="Bankverbindung",
    account_holder="Kontoinhaber",
    iban="IBAN",
    routing="BLZ",
    iban_country="DE",
    iban_length=22,
    notes="Hinweise",
    page="Seite {page} von {total}",
    continued="Fortsetzung",
)

_NL: Final = _Labels(
    lang="nl",
    title_invoice="FACTUUR",
    title_credit="CREDITFACTUUR",
    tax_id="BTW-nummer",
    bill_to="Factuuradres",
    invoice_number="Factuurnummer",
    issue_date="Factuurdatum",
    due_date="Vervaldatum",
    po_number="Ordernummer",
    currency="Valuta",
    col_pos="Pos.",
    col_sku="Artikelnr.",
    col_description="Omschrijving",
    col_quantity="Aantal",
    col_unit="Eenheid",
    col_unit_price="Prijs p/e",
    col_amount="Bedrag",
    subtotal="Subtotaal netto",
    tax_line="Te vermeerderen met BTW",
    table_total="Totaalbedrag",
    summary_total="Factuurbedrag",
    summary_credit="Creditbedrag",
    payable_by="Betalen vóór",
    settle_by="Verrekening per",
    payment_terms="Betalingsvoorwaarden",
    terms_sentence=(
        "Het factuurbedrag dient zonder aftrek vóór {due} te worden overgemaakt op "
        "onderstaand rekeningnummer. Vermeld bij betaling het factuurnummer "
        "{number} als betalingskenmerk."
    ),
    terms_sentence_credit=(
        "Het creditbedrag wordt per {due} verrekend met openstaande facturen. "
        "Uitbetaling vindt uitsluitend plaats op schriftelijk verzoek onder vermelding "
        "van creditnummer {number}."
    ),
    bank_box="Bankgegevens",
    account_holder="Rekeninghouder",
    iban="IBAN",
    routing="Bankcode",
    iban_country="NL",
    iban_length=18,
    notes="Opmerkingen",
    page="Pagina {page} van {total}",
    continued="Vervolg",
)


#: IBAN country code and total length, keyed by the prefix of the vendor's tax
#: identifier. A Swiss vendor must not be given a German IBAN just because the
#: document happens to be written in German.
_IBAN_BY_COUNTRY: Final[dict[str, tuple[str, int]]] = {
    "DE": ("DE", 22),
    "AT": ("AT", 20),
    "CH": ("CH", 21),
    "CHE": ("CH", 21),
    "NL": ("NL", 18),
    "BE": ("BE", 16),
    "LU": ("LU", 20),
    "FR": ("FR", 27),
    "IT": ("IT", 27),
    "ES": ("ES", 24),
    "PL": ("PL", 28),
    "DK": ("DK", 18),
    "SE": ("SE", 24),
}

#: Tax-id prefixes that fix the document's language. Everything else falls back
#: to the seeded coin flip.
_DUTCH_PREFIXES: Final[frozenset[str]] = frozenset({"NL", "BE"})
_GERMAN_PREFIXES: Final[frozenset[str]] = frozenset({"DE", "AT", "CH"})


def _country_prefix(vendor_tax_id: str) -> str:
    letters = "".join(char for char in vendor_tax_id if char.isalpha()).upper()
    return letters[:3]


def _choose_labels(spec: InvoiceSpec, rng: random.Random) -> _Labels:
    """German or Dutch, inferred from the vendor's tax identifier where it says so.

    ``DE…``/``ATU…``/``CHE…`` are German-language jurisdictions for this purpose,
    ``NL…``/``BE…`` Dutch. When the identifier carries no usable country prefix the
    choice falls back to the seeded coin flip, so it stays deterministic either way.
    """
    # The coin is always flipped, whether or not it is consulted, so that adding a
    # country to the table never shifts the random stream the rest of the layout
    # draws from.
    coin = _DE if rng.random() < 0.55 else _NL
    prefix = _country_prefix(spec.vendor_tax_id)[:2]
    if prefix in _DUTCH_PREFIXES:
        return _NL
    if prefix in _GERMAN_PREFIXES:
        return _DE
    return coin


def _iban_shape(vendor_tax_id: str, labels: _Labels) -> tuple[str, int]:
    """The IBAN country prefix and length to mask against.

    Taken from the vendor's own tax identifier where it names a country — a Swiss
    supplier writing in German must not be handed a 22-character DE IBAN — and
    otherwise from the document language's default.
    """
    prefix = _country_prefix(vendor_tax_id)
    for candidate in (prefix, prefix[:2]):
        if candidate in _IBAN_BY_COUNTRY:
            return _IBAN_BY_COUNTRY[candidate]
    return labels.iban_country, labels.iban_length


# --------------------------------------------------------------------------- #
# Formatting. Decimal in, str out, and never a float anywhere in between.
# --------------------------------------------------------------------------- #


def _uses_european_punctuation(currency: str) -> bool:
    """EUR is the trigger the contract names; the other euro-zone-adjacent codes
    that genuinely use ``1.234,56`` in commercial documents ride along."""
    return currency.upper() in {"EUR", "CHF", "DKK", "NOK", "SEK", "PLN", "CZK"}


def _group_thousands(digits: str, sep: str) -> str:
    out: list[str] = []
    for index, char in enumerate(reversed(digits)):
        if index and index % 3 == 0:
            out.append(sep)
        out.append(char)
    return "".join(reversed(out))


def _format_decimal(value: Decimal, places: int, *, european: bool) -> str:
    """Render a Decimal at a fixed number of places in the document's punctuation.

    Negative values keep a leading minus, which is how a European credit note
    prints its figures.
    """
    quantum = Decimal(1).scaleb(-places)
    quantised = value.quantize(quantum, rounding=ROUND_HALF_UP)
    sign = "-" if quantised < 0 else ""
    digits = format(abs(quantised), "f")
    if "." in digits:
        whole, frac = digits.split(".", 1)
    else:
        whole, frac = digits, ""
    frac = frac.ljust(places, "0")[:places]
    thousands_sep, decimal_sep = (".", ",") if european else (",", ".")
    grouped = _group_thousands(whole, thousands_sep)
    if places == 0:
        return f"{sign}{grouped}"
    return f"{sign}{grouped}{decimal_sep}{frac}"


def _decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # 'n', 'N' or 'F' for NaN / Infinity
        return 2
    return max(0, -exponent)


def _format_money(value: Decimal, *, european: bool) -> str:
    return _format_decimal(value, 2, european=european)


def _format_unit_price(value: Decimal, *, european: bool) -> str:
    """Industrial suppliers quote cheap bulk parts to four decimals.

    Continental price lists use two places or four, never three, so a spec that
    carries sub-cent precision snaps up to four rather than printing whatever
    precision the Decimal happens to have.
    """
    return _format_decimal(value, 2 if _decimal_places(value) <= 2 else 4, european=european)


def _format_quantity(value: Decimal, *, european: bool) -> str:
    return _format_decimal(value, min(3, max(2, _decimal_places(value))), european=european)


def _format_rate_percent(tax_rate: Decimal, *, european: bool) -> str:
    """``tax_rate`` is stated either as a fraction (0.19) or as a percentage (19).

    A rate at or below 1 is read as a fraction, which is the only reading that
    makes sense: no real VAT regime charges one percent, and none of the European
    rates a fixture would carry (7, 9, 19, 20, 21, 25) fall below 1.
    """
    percent = tax_rate * 100 if abs(tax_rate) <= 1 else tax_rate
    return f"{_format_decimal(percent, 2, european=european)} %"


def _format_date(value: date) -> str:
    return f"{value.day:02d}.{value.month:02d}.{value.year:04d}"


def _masked_iban(last4: str, country: str, length: int) -> str:
    """Group an ``XXXX``-masked IBAN of the right national length in fours.

    The fixtures only ever carry the last four digits on purpose (see the spec's
    own note), so everything ahead of them is masked rather than invented.
    """
    tail = "".join(char for char in last4 if char.isalnum())[-4:].rjust(4, "X")
    body = country + "X" * max(0, length - len(country) - len(tail)) + tail
    return " ".join(body[i : i + 4] for i in range(0, len(body), 4))


def _masked_routing(last4: str) -> str:
    tail = "".join(char for char in last4 if char.isalnum())[-4:].rjust(4, "X")
    return f"XXXX {tail}"


# --------------------------------------------------------------------------- #
# Text measurement and wrapping.
# --------------------------------------------------------------------------- #


def _text_width(text: str, font: str, size: float) -> float:
    return float(stringWidth(text, font, size))


def _ellipsise(text: str, font: str, size: float, max_width: float) -> str:
    """Trim a single-line string to fit, with a trailing ellipsis if it had to give."""
    if max_width <= 0.0:
        return ""
    if _text_width(text, font, size) <= max_width:
        return text
    ellipsis = "…"
    budget = max_width - _text_width(ellipsis, font, size)
    kept = ""
    for char in text:
        if _text_width(kept + char, font, size) > budget:
            break
        kept += char
    return kept.rstrip(" ·,-") + ellipsis


def _wrap(text: str, font: str, size: float, max_width: float) -> tuple[str, ...]:
    """Greedy word wrap with a character-level fallback for unbreakable tokens.

    Article descriptions in this family are long by design; this is what binds
    their continuation lines to their own table row.
    """
    if not text.strip():
        return ("",)
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if _text_width(candidate, font, size) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if _text_width(word, font, size) <= max_width:
            current = word
            continue
        chunk = ""
        for char in word:
            if _text_width(chunk + char, font, size) <= max_width or not chunk:
                chunk += char
            else:
                lines.append(chunk)
                chunk = char
        current = chunk
    if current:
        lines.append(current)
    return tuple(lines)


# --------------------------------------------------------------------------- #
# Row model. Heights are computed before anything is drawn so pagination and the
# "Seite 1 von 3" footer can both be right.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Row:
    cells: tuple[tuple[str, ...], ...]
    height: float


def _row_height(cells: tuple[tuple[str, ...], ...]) -> float:
    tallest = max(len(lines) for lines in cells)
    return max(15.0, tallest * _LEAD + 2 * _CELL_PAD_Y)


def _build_item_row(index: int, item: LineItem, labels: _Labels, *, european: bool) -> _Row:
    description = _wrap(item.description, _FONT, _BODY, _COL_W[2] - 2 * _CELL_PAD_X)
    sku = _wrap(item.sku or "", _FONT, _BODY, _COL_W[1] - 2 * _CELL_PAD_X)
    cells: tuple[tuple[str, ...], ...] = (
        (f"{index:02d}",),
        sku,
        description,
        (_format_quantity(item.quantity, european=european),),
        (item.unit_of_measure,),
        (_format_unit_price(item.unit_price, european=european),),
        (_format_money(item.line_total, european=european),),
    )
    del labels  # language does not change an article row's content
    return _Row(cells=cells, height=_row_height(cells))


# --------------------------------------------------------------------------- #
# Primitive drawing helpers.
# --------------------------------------------------------------------------- #


def _draw_cell_text(
    canvas: Any,
    x: float,
    width: float,
    y_top: float,
    lines: tuple[str, ...],
    align: str,
    font: str,
    size: float,
) -> None:
    baseline = y_top - _CELL_PAD_Y - size
    canvas.setFont(font, size)
    for line in lines:
        if line:
            if align == "r":
                canvas.drawRightString(x + width - _CELL_PAD_X, baseline, line)
            elif align == "c":
                canvas.drawCentredString(x + width / 2.0, baseline, line)
            else:
                canvas.drawString(x + _CELL_PAD_X, baseline, line)
        baseline -= _LEAD


def _draw_table_row(
    canvas: Any,
    y_top: float,
    row: _Row,
    *,
    font: str,
    size: float,
    fill: float | None,
    rule: float,
    bottom_rule: float,
) -> float:
    """Draw one fully-ruled row and return the y of its bottom edge."""
    y_bottom = y_top - row.height
    if fill is not None:
        canvas.setFillGray(fill)
        canvas.rect(_MARGIN_L, y_bottom, _CONTENT_W, row.height, stroke=0, fill=1)
    canvas.setFillGray(_INK if fill is None or fill > 0.5 else 1.0)

    x = _MARGIN_L
    for width, lines, align in zip(_COL_W, row.cells, _ALIGN, strict=True):
        _draw_cell_text(canvas, x, width, y_top, lines, align, font, size)
        x += width

    canvas.setFillGray(_INK)
    canvas.setStrokeGray(_GREY_RULE if rule <= 0.4 else 0.0)
    canvas.setLineWidth(rule)
    x = _MARGIN_L
    for width in _COL_W:
        canvas.line(x, y_top, x, y_bottom)
        x += width
    canvas.line(x, y_top, x, y_bottom)
    canvas.setStrokeGray(0.0)
    canvas.setLineWidth(bottom_rule)
    canvas.line(_MARGIN_L, y_bottom, _MARGIN_L + _CONTENT_W, y_bottom)
    return y_bottom


def _draw_label_value_grid(
    canvas: Any,
    x: float,
    y_top: float,
    rows: tuple[tuple[str, str], ...],
    label_w: float,
    value_w: float,
) -> float:
    """The metadata block beside the address — label left, value right-aligned."""
    y = y_top
    for label, value in rows:
        canvas.setFont(_FONT, _BODY)
        canvas.drawString(x, y - _BODY, f"{label}")
        canvas.setFont(_FONT_B, _BODY)
        canvas.drawRightString(x + label_w + value_w, y - _BODY, value)
        canvas.setStrokeGray(0.78)
        canvas.setLineWidth(0.3)
        canvas.line(x, y - _BODY - 3.4, x + label_w + value_w, y - _BODY - 3.4)
        canvas.setStrokeGray(0.0)
        y -= _LEAD + 3.6
    return y


# --------------------------------------------------------------------------- #
# Document sections.
# --------------------------------------------------------------------------- #


def _draw_header(canvas: Any, spec: InvoiceSpec, labels: _Labels, band: float) -> float:
    """Vendor block, tax identifier, title, addressee and metadata grid.

    The tax identifier sits in the header band, which is the continental habit —
    a US invoice would put an EIN in the footer if it printed one at all.
    """
    y = _PAGE_H - _MARGIN_T

    canvas.setFillGray(band)
    canvas.rect(_MARGIN_L, y - 30.0, _CONTENT_W, 30.0, stroke=0, fill=1)
    canvas.setFillGray(1.0 if band < 0.5 else 0.0)
    canvas.setFont(_FONT_B, 14.0)
    canvas.drawString(_MARGIN_L + 8.0, y - 20.0, spec.vendor_name.upper())
    canvas.setFont(_FONT, _SMALL)
    canvas.drawRightString(
        _MARGIN_L + _CONTENT_W - 8.0, y - 20.0, f"{labels.tax_id}: {spec.vendor_tax_id}"
    )
    canvas.setFillGray(_INK)
    y -= 30.0 + 8.0

    canvas.setFont(_FONT, _SMALL)
    for line in spec.vendor_address_lines:
        canvas.drawString(_MARGIN_L, y - _SMALL, line)
        y -= _SMALL + 1.9
    y -= 12.0

    title = labels.title_credit if spec.is_credit_note else labels.title_invoice
    canvas.setFont(_FONT_B, 17.0)
    canvas.drawString(_MARGIN_L, y - 17.0, title)
    canvas.setFont(_FONT, 11.0)
    canvas.drawString(
        _MARGIN_L + _text_width(title, _FONT_B, 17.0) + 10.0,
        y - 17.0,
        spec.invoice_number,
    )
    y -= 17.0 + 12.0

    block_top = y
    canvas.setFont(_FONT_B, _SMALL)
    canvas.drawString(_MARGIN_L, block_top - _SMALL, labels.bill_to.upper())
    address_y = block_top - _SMALL - 8.0
    canvas.setFont(_FONT, _BODY)
    for line in spec.bill_to_lines:
        canvas.drawString(_MARGIN_L, address_y - _BODY, line)
        address_y -= _LEAD

    meta_rows: list[tuple[str, str]] = [
        (labels.invoice_number, spec.invoice_number),
        (labels.issue_date, _format_date(spec.issue_date)),
        (labels.due_date, _format_date(spec.due_date)),
    ]
    if spec.po_number is not None:
        meta_rows.append((labels.po_number, spec.po_number))
    meta_rows.append((labels.currency, spec.currency.upper()))

    meta_w = 216.0
    meta_x = _MARGIN_L + _CONTENT_W - meta_w
    meta_y = _draw_label_value_grid(
        canvas, meta_x, block_top, tuple(meta_rows), 96.0, meta_w - 96.0
    )

    return min(address_y, meta_y) - 14.0


def _draw_table_header(
    canvas: Any, y_top: float, labels: _Labels, currency: str, band: float
) -> float:
    money_suffix = f" {currency.upper()}"
    header = _Row(
        cells=(
            (labels.col_pos,),
            (labels.col_sku,),
            (labels.col_description,),
            (labels.col_quantity,),
            (labels.col_unit,),
            (labels.col_unit_price + money_suffix,),
            (labels.col_amount + money_suffix,),
        ),
        height=17.0,
    )
    canvas.setStrokeGray(0.0)
    canvas.setLineWidth(0.9)
    canvas.line(_MARGIN_L, y_top, _MARGIN_L + _CONTENT_W, y_top)
    return _draw_table_row(
        canvas,
        y_top,
        header,
        font=_FONT_B,
        size=_SMALL,
        fill=band,
        rule=0.9,
        bottom_rule=0.9,
    )


def _draw_totals_block(
    canvas: Any,
    y_top: float,
    spec: InvoiceSpec,
    labels: _Labels,
    *,
    european: bool,
) -> float:
    """Subtotal, the VAT rate on a line of its own, and the first printing of the
    total as the closing row of the ruled table."""
    label_x = _MARGIN_L + sum(_COL_W[:2])
    label_w = sum(_COL_W[2:5])
    amount_x = _MARGIN_L + sum(_COL_W[:5])
    amount_w = _COL_W[5] + _COL_W[6]

    rows: tuple[tuple[str, str, bool, float], ...] = (
        (labels.subtotal, _format_money(spec.subtotal, european=european), False, 0.35),
        (
            f"{labels.tax_line} {_format_rate_percent(spec.tax_rate, european=european)}",
            _format_money(spec.tax_amount, european=european),
            False,
            0.9,
        ),
        (labels.table_total, _format_money(spec.total, european=european), True, 1.4),
    )

    y = y_top
    for label, amount, bold, bottom_rule in rows:
        height = _TOTALS_TOTAL_ROW_H if bold else _TOTALS_ROW_H
        y_bottom = y - height
        font = _FONT_B if bold else _FONT
        size = _BODY if not bold else 9.6
        if bold:
            canvas.setFillGray(0.90)
            canvas.rect(label_x, y_bottom, label_w + amount_w, height, stroke=0, fill=1)
            canvas.setFillGray(_INK)
        canvas.setFont(font, size)
        canvas.drawString(label_x + _CELL_PAD_X, y_bottom + _CELL_PAD_Y + 1.6, label)
        canvas.setFont(font, size)
        canvas.drawRightString(
            amount_x + amount_w - _CELL_PAD_X, y_bottom + _CELL_PAD_Y + 1.6, amount
        )
        canvas.setStrokeGray(0.0)
        canvas.setLineWidth(0.6)
        canvas.line(label_x, y, label_x, y_bottom)
        canvas.line(amount_x, y, amount_x, y_bottom)
        canvas.line(label_x + label_w + amount_w, y, label_x + label_w + amount_w, y_bottom)
        canvas.setLineWidth(bottom_rule)
        canvas.line(label_x, y_bottom, label_x + label_w + amount_w, y_bottom)
        y = y_bottom
    return y


def _draw_summary_box(
    canvas: Any,
    y_top: float,
    spec: InvoiceSpec,
    labels: _Labels,
    *,
    european: bool,
    doubled: bool,
) -> float:
    """The second printing of the total, in a box of its own."""
    box_w = _SUMMARY_W
    box_h = _SUMMARY_H
    x = _MARGIN_L + _CONTENT_W - box_w
    y_bottom = y_top - box_h

    if doubled:
        canvas.setStrokeGray(0.0)
        canvas.setLineWidth(0.9)
        canvas.rect(x, y_bottom, box_w, box_h, stroke=1, fill=0)
        canvas.setLineWidth(0.35)
        canvas.rect(x + 2.6, y_bottom + 2.6, box_w - 5.2, box_h - 5.2, stroke=1, fill=0)
    else:
        canvas.setFillGray(0.92)
        canvas.rect(x, y_bottom, box_w, box_h, stroke=0, fill=1)
        canvas.setFillGray(_INK)
        canvas.setStrokeGray(0.0)
        canvas.setLineWidth(1.2)
        canvas.rect(x, y_bottom, box_w, box_h, stroke=1, fill=0)

    credit = spec.is_credit_note
    canvas.setFillGray(_INK)
    canvas.setFont(_FONT_B, _SMALL)
    canvas.drawString(
        x + 9.0,
        y_top - 14.0,
        (labels.summary_credit if credit else labels.summary_total).upper(),
    )
    canvas.setFont(_FONT_B, 13.5)
    canvas.drawRightString(
        x + box_w - 9.0,
        y_top - 15.4,
        f"{spec.currency.upper()} {_format_money(spec.total, european=european)}",
    )
    canvas.setFont(_FONT, _SMALL)
    canvas.drawString(
        x + 9.0,
        y_top - 32.0,
        f"{labels.settle_by if credit else labels.payable_by} {_format_date(spec.due_date)}",
    )
    canvas.drawRightString(x + box_w - 9.0, y_top - 32.0, spec.invoice_number)
    return y_bottom


def _terms_blocks(
    spec: InvoiceSpec, labels: _Labels, width: float
) -> tuple[tuple[str, str, float, tuple[str, ...]], ...]:
    """(heading, font, size, wrapped body lines) for the terms and notes stack.

    Built once and measured before it is drawn, so pagination knows exactly how
    much room the tail of the document needs.
    """
    template = labels.terms_sentence_credit if spec.is_credit_note else labels.terms_sentence
    sentence = template.format(due=_format_date(spec.due_date), number=spec.invoice_number)
    blocks: list[tuple[str, str, float, tuple[str, ...]]] = [
        (labels.payment_terms, _FONT, _BODY, _wrap(spec.payment_terms, _FONT, _BODY, width)),
        ("", _FONT_O, _BODY, _wrap(sentence, _FONT_O, _BODY, width)),
    ]
    if spec.notes is not None and spec.notes.strip():
        blocks.append((labels.notes, _FONT, _BODY, _wrap(spec.notes, _FONT, _BODY, width)))
    return tuple(blocks)


def _terms_height(blocks: tuple[tuple[str, str, float, tuple[str, ...]], ...]) -> float:
    height = 0.0
    for index, (heading, _font, _size, lines) in enumerate(blocks):
        if heading:
            height += _SMALL + 5.0 + (6.0 if index else 0.0)
        height += _LEAD * len(lines)
    return height


def _draw_terms_and_notes(
    canvas: Any, y_top: float, blocks: tuple[tuple[str, str, float, tuple[str, ...]], ...]
) -> float:
    y = y_top
    for index, (heading, font, size, lines) in enumerate(blocks):
        if heading:
            if index:
                y -= 6.0
            canvas.setFont(_FONT_B, _SMALL)
            canvas.drawString(_MARGIN_L, y - _SMALL, heading.upper())
            y -= _SMALL + 5.0
        canvas.setFont(font, size)
        for line in lines:
            canvas.drawString(_MARGIN_L, y - size, line)
            y -= _LEAD
    return y


def _draw_bank_box(canvas: Any, y_top: float, spec: InvoiceSpec, labels: _Labels) -> float:
    box_h = _BANK_BOX_H
    y_bottom = y_top - box_h
    canvas.setStrokeGray(0.0)
    canvas.setLineWidth(0.7)
    canvas.rect(_MARGIN_L, y_bottom, _CONTENT_W, box_h, stroke=1, fill=0)
    canvas.setFillGray(0.90)
    canvas.rect(_MARGIN_L, y_top - 14.0, _CONTENT_W, 14.0, stroke=0, fill=1)
    canvas.setFillGray(_INK)
    canvas.setStrokeGray(0.0)
    canvas.setLineWidth(0.7)
    canvas.line(_MARGIN_L, y_top - 14.0, _MARGIN_L + _CONTENT_W, y_top - 14.0)

    canvas.setFont(_FONT_B, _SMALL)
    canvas.drawString(_MARGIN_L + 8.0, y_top - 10.4, labels.bank_box.upper())
    canvas.drawRightString(
        _MARGIN_L + _CONTENT_W - 8.0, y_top - 10.4, f"{labels.tax_id}: {spec.vendor_tax_id}"
    )

    iban_country, iban_length = _iban_shape(spec.vendor_tax_id, labels)
    iban = _masked_iban(spec.bank_iban_last4, iban_country, iban_length)
    left_x = _MARGIN_L + 8.0
    right_x = _MARGIN_L + _CONTENT_W / 2.0 + 8.0
    line_y = y_top - 26.0

    canvas.setFont(_FONT, _SMALL)
    canvas.drawString(left_x, line_y, f"{labels.account_holder}:")
    canvas.setFont(_FONT_B, _SMALL)
    canvas.drawString(left_x + 62.0, line_y, spec.vendor_name)

    canvas.setFont(_FONT, _SMALL)
    canvas.drawString(right_x, line_y, f"{labels.iban}:")
    canvas.setFont(_FONT_B, _SMALL)
    canvas.drawString(right_x + 34.0, line_y, iban)

    line_y -= 11.5
    canvas.setFont(_FONT, _SMALL)
    canvas.drawString(left_x, line_y, f"{labels.currency}:")
    canvas.setFont(_FONT_B, _SMALL)
    canvas.drawString(left_x + 62.0, line_y, spec.currency.upper())

    canvas.setFont(_FONT, _SMALL)
    canvas.drawString(right_x, line_y, f"{labels.routing}:")
    canvas.setFont(_FONT_B, _SMALL)
    canvas.drawString(right_x + 34.0, line_y, _masked_routing(spec.bank_routing_last4))
    return y_bottom


def _draw_page_footer(
    canvas: Any, spec: InvoiceSpec, labels: _Labels, page: int, total_pages: int
) -> None:
    y = _MARGIN_B - 18.0
    canvas.setStrokeGray(0.6)
    canvas.setLineWidth(0.3)
    canvas.line(_MARGIN_L, y + 11.0, _MARGIN_L + _CONTENT_W, y + 11.0)
    canvas.setStrokeGray(0.0)
    canvas.setFillGray(0.25)
    canvas.setFont(_FONT, _SMALL)
    centre = (
        f"{labels.title_credit if spec.is_credit_note else labels.title_invoice} "
        f"{spec.invoice_number}"
    )
    # The three footer slots share one line; the left one yields so a long vendor
    # name never runs into the document reference in the middle.
    left_budget = _CONTENT_W / 2.0 - _text_width(centre, _FONT, _SMALL) / 2.0 - 10.0
    canvas.drawString(
        _MARGIN_L,
        y,
        _ellipsise(
            f"{spec.vendor_name} · {labels.tax_id} {spec.vendor_tax_id}",
            _FONT,
            _SMALL,
            left_budget,
        ),
    )
    canvas.drawCentredString(_MARGIN_L + _CONTENT_W / 2.0, y, centre)
    canvas.drawRightString(
        _MARGIN_L + _CONTENT_W,
        y,
        labels.page.format(page=page, total=total_pages),
    )
    canvas.setFillGray(_INK)


# --------------------------------------------------------------------------- #
# Composition.
# --------------------------------------------------------------------------- #


def _bottom_limit() -> float:
    return _MARGIN_B + 4.0


def _draw_continuation_banner(canvas: Any, spec: InvoiceSpec, labels: _Labels) -> float:
    """Head a spill page so it is still identifiable on its own.

    A page of article rows with no masthead is unattributable once it is separated
    from page one, which is exactly what happens in a scanning room. Continental
    suppliers repeat the document reference and the tax identifier instead.
    """
    y = _PAGE_H - _MARGIN_T
    title = labels.title_credit if spec.is_credit_note else labels.title_invoice
    canvas.setFont(_FONT_B, 10.0)
    canvas.drawString(_MARGIN_L, y - 10.0, f"{title} {spec.invoice_number} — {labels.continued}")
    canvas.setFont(_FONT, _SMALL)
    canvas.drawRightString(
        _MARGIN_L + _CONTENT_W, y - 10.0, f"{labels.tax_id}: {spec.vendor_tax_id}"
    )
    return y - 22.0


def _paint(canvas: Any, spec: InvoiceSpec, total_pages: int) -> int:
    """Lay the whole document out, drawing as it goes, and return the page count.

    Called twice: once against a throwaway buffer to learn the page count so the
    "Seite 1 von 3" footer can be truthful, then once for real.
    """
    rng = random.Random(spec.seed)
    labels = _choose_labels(spec, rng)
    band = rng.choice((0.0, 0.14, 0.86))
    doubled_summary = rng.random() < 0.5
    zebra = rng.random() < 0.4

    european = _uses_european_punctuation(spec.currency)
    # The terms column stops short of the summary box's left edge so the two
    # never collide when the box sits high on the page.
    terms = _terms_blocks(spec, labels, _CONTENT_W - _SUMMARY_W - 12.0)

    rows = tuple(
        _build_item_row(index, item, labels, european=european)
        for index, item in enumerate(spec.line_items, start=1)
    )

    page = 1
    y = _draw_header(canvas, spec, labels, band)
    y = _draw_table_header(canvas, y, labels, spec.currency, band)

    for position, row in enumerate(rows):
        if y - row.height < _bottom_limit():
            _draw_page_footer(canvas, spec, labels, page, total_pages)
            canvas.showPage()
            page += 1
            y = _draw_continuation_banner(canvas, spec, labels)
            y = _draw_table_header(canvas, y, labels, spec.currency, band)
        fill = 0.955 if (zebra and position % 2 == 1) else None
        y = _draw_table_row(
            canvas,
            y,
            row,
            font=_FONT,
            size=_BODY,
            fill=fill,
            rule=0.35,
            bottom_rule=0.35,
        )

    # Totals block, summary box and terms travel together: a continental invoice
    # never orphans the VAT breakdown from the figure it explains. The bank box is
    # pinned to the foot of the page, so it is what the tail must clear.
    bank_top = _bottom_limit() + _BANK_BOX_H + 10.0
    tail_height = _TOTALS_H + 12.0 + _SUMMARY_H + 16.0 + _terms_height(terms) + 12.0
    if y - tail_height < bank_top:
        _draw_page_footer(canvas, spec, labels, page, total_pages)
        canvas.showPage()
        page += 1
        y = _draw_continuation_banner(canvas, spec, labels)

    y = _draw_totals_block(canvas, y, spec, labels, european=european)
    y -= 12.0
    y = _draw_summary_box(canvas, y, spec, labels, european=european, doubled=doubled_summary)
    y -= 16.0
    _draw_terms_and_notes(canvas, y, terms)
    _draw_bank_box(canvas, bank_top, spec, labels)
    _draw_page_footer(canvas, spec, labels, page, total_pages)
    canvas.showPage()
    return page


def _new_canvas(target: Any) -> Any:
    """A canvas with every non-deterministic default suppressed.

    ``invariant=True`` pins reportlab's ``/CreationDate`` and document ``/ID``;
    without it two renders of the same spec differ in bytes. The producer string
    is pinned too so the fixture bytes do not move when reportlab is upgraded.
    """
    canvas = Canvas(target, pagesize=A4, invariant=True, pageCompression=1)
    canvas.setProducer(f"threshold-seeds/{LAYOUT_NAME}")
    return canvas


def render(spec: InvoiceSpec, path: Path) -> None:
    """Render ``spec`` as a continental tabular invoice PDF at ``path``.

    The spec is never mutated. Two calls with the same spec produce byte-identical
    files.
    """
    probe = io.BytesIO()
    total_pages = _paint(_new_canvas(probe), spec, total_pages=1)

    canvas = _new_canvas(str(path))
    canvas.setTitle(f"{'Gutschrift' if spec.is_credit_note else 'Rechnung'} {spec.invoice_number}")
    canvas.setAuthor(spec.vendor_name)
    canvas.setSubject(f"{spec.currency.upper()} {spec.total}")
    canvas.setCreator(f"threshold-seeds/{LAYOUT_NAME}")
    _paint(canvas, spec, total_pages=total_pages)
    canvas.save()
