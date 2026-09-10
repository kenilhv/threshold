"""The always-available extraction tier.

Text layer plus labelled-pattern matching. It is genuinely worse than a vision
model on scanned documents — it cannot read a page that has no text layer at
all — and it is labelled worse everywhere its output is stored.

It exists for three reasons, in order of importance:

1. **CI runs the real pipeline.** Every commit exercises ingest, extraction,
   persistence and the confidence gate hermetically: no weights, no API key, no
   network. The alternative is mocking away the one component whose behaviour
   the system is built around.
2. **It is the floor.** Any evaluation of the model tier is measured against
   this, so "the model helps" becomes a number rather than an assumption.
3. **It degrades rather than fails.** When a provider is down, invoices with a
   clean text layer keep flowing at reduced confidence instead of the queue
   stopping.

## How confidence is assigned

Confidence here is *earned from observable signal*, never a constant. A
hard-coded 0.9 would be a decoration that the calibration work in M6 would
immediately expose as meaningless. The signals used:

* **Label adjacency.** A value found next to a recognised label scores higher
  than the same pattern found loose in the page. "Total: 1,234.56" is stronger
  evidence than a bare "1,234.56" in a footer.
* **Corroboration.** If subtotal + tax reconciles to the total within a cent,
  all three are raised. Internal consistency is real evidence and it is free.
* **Competition.** When several candidates match one field, confidence drops as
  a function of how many plausible alternatives were rejected. Ambiguity is a
  reason to be less sure, and most extractors forget to model it.
* **Format certainty.** An ISO date is less ambiguous than 03/04/2026, which is
  genuinely two different dates depending on the continent it was printed on.
"""

from __future__ import annotations

import io
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

import structlog
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from threshold.adapters.extraction.port import ExtractionError
from threshold.domain.extraction import (
    ExtractedField,
    ExtractedInvoice,
    ExtractedLineItem,
    ExtractionResult,
    ExtractorKind,
)

log = structlog.get_logger(__name__)

#: Bumped whenever the rules below change in a way that could move a result.
#: Stored on every extraction so a shift in the numbers is attributable.
EXTRACTOR_VERSION = "deterministic/1.1.0"

# --------------------------------------------------------------------- labels

_INVOICE_NUMBER_LABELS = r"(?:invoice\s*(?:no\.?|number|#)|rechnungsnummer|factuurnummer|inv\s*#)"
_PO_LABELS = r"(?:p\.?o\.?\s*(?:no\.?|number|#)?|purchase\s*order|bestellnummer|order\s*no\.?)"
_TOTAL_LABELS = r"(?:total\s*(?:due|amount)?|amount\s*due|balance\s*due|gesamtbetrag|totaal)"
_SUBTOTAL_LABELS = r"(?:sub\s*-?\s*total|zwischensumme|subtotaal|net\s*amount)"
_TAX_LABELS = r"(?:tax|vat|mwst\.?|btw|sales\s*tax)"
_TAX_ID_LABELS = r"(?:tax\s*id|vat\s*(?:id|no\.?|number)|ust-?idnr\.?|ein|abn)"
_ISSUE_DATE_LABELS = r"(?:invoice\s*date|date\s*of\s*issue|issued?|rechnungsdatum|factuurdatum)"
_DUE_DATE_LABELS = r"(?:due\s*date|payment\s*due|fällig|vervaldatum)"

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "SEK", "NOK", "DKK"}


class DeterministicExtractor:
    """Reads the PDF text layer and matches labelled patterns."""

    @property
    def extractor_id(self) -> str:
        return EXTRACTOR_VERSION

    async def extract(self, content: bytes, content_type: str) -> ExtractionResult:
        started = time.perf_counter()

        text, page_count = _read_text(content, content_type)
        if not text.strip():
            # A PDF with no text layer is the scanned case. Not an error — an
            # honest zero-confidence result that routes straight to a human,
            # which is exactly what should happen until the model tier exists.
            log.info("extract.no_text_layer", pages=page_count)
            return _empty_result(started, reason="no_text_layer")

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        haystack = "\n".join(lines)

        currency, currency_conf = _find_currency(haystack)
        subtotal = _find_labelled_amount(lines, _SUBTOTAL_LABELS, currency)
        tax_amount = _find_labelled_amount(lines, _TAX_LABELS, currency)
        total = _find_labelled_amount(lines, _TOTAL_LABELS, currency)

        subtotal, tax_amount, total = _corroborate_totals(subtotal, tax_amount, total)

        doc_type, doc_type_conf = _classify_document(haystack, total.value)

        invoice = ExtractedInvoice(
            document_type=ExtractedField(value=doc_type, confidence=doc_type_conf),
            invoice_number=_find_labelled_token(lines, _INVOICE_NUMBER_LABELS),
            vendor_name=_find_vendor_name(lines),
            vendor_tax_id=_find_labelled_token(lines, _TAX_ID_LABELS),
            po_number=_find_labelled_token(lines, _PO_LABELS),
            issue_date=_find_labelled_date(lines, _ISSUE_DATE_LABELS),
            due_date=_find_labelled_date(lines, _DUE_DATE_LABELS),
            currency=ExtractedField(value=currency, confidence=currency_conf),
            subtotal=subtotal,
            tax_amount=tax_amount,
            total=total,
            bank_account_last4=_find_masked_last4(lines, _BANK_ACCOUNT_LABELS),
            bank_routing_last4=_find_masked_last4(lines, _BANK_ROUTING_LABELS),
            line_items=_find_line_items(lines),
        )

        return ExtractionResult(
            invoice=invoice,
            extractor_kind=ExtractorKind.DETERMINISTIC,
            extractor_id=EXTRACTOR_VERSION,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


# ------------------------------------------------------------------ reading --


def _read_text(content: bytes, content_type: str) -> tuple[str, int]:
    if content_type not in ("application/pdf", "application/x-pdf"):
        raise ExtractionError(f"unsupported content type for this tier: {content_type}")
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = reader.pages
        return "\n".join(page.extract_text() or "" for page in pages), len(pages)
    except (PdfReadError, OSError, ValueError) as exc:
        raise ExtractionError("could not read the document as a PDF") from exc


# ------------------------------------------------------------------ numbers --


def _parse_amount(raw: str) -> Decimal | None:
    """Parse US and European conventions without guessing between them blindly.

    1.234,56 and 1,234.56 are the same number written by different continents.
    The rule: whichever separator appears LAST is the decimal separator. It is
    unambiguous whenever both are present, and falls back to position and group
    size when only one is.
    """
    cleaned = re.sub(r"[^\d.,\-()]", "", raw).strip()
    if not cleaned:
        return None

    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]

    last_dot, last_comma = cleaned.rfind("."), cleaned.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        if last_comma > last_dot:  # 1.234,56 — European
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:  # 1,234.56 — US
            cleaned = cleaned.replace(",", "")
    elif last_comma >= 0:
        # A lone comma is a decimal separator unless it groups exactly three
        # digits at the end, which is the thousands case.
        cleaned = (
            cleaned.replace(",", "")
            if re.search(r",\d{3}$", cleaned)
            else cleaned.replace(",", ".")
        )
    elif last_dot >= 0 and re.search(r"\.\d{3}$", cleaned) and cleaned.count(".") == 1:
        # 1.234 with no other separator is ambiguous. Treated as thousands,
        # which is the more common intent on a European invoice.
        cleaned = cleaned.replace(".", "")

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


_AMOUNT_PATTERN = re.compile(r"[-(]?\s*[\d][\d.,\s]{0,18}\d(?:\s*\))?")
_PERCENT_SUFFIX = re.compile(r"\s*%")


# ------------------------------------------------------------- line items --

#: A record starts with its position number on a line of its own. Every tabular
#: layout here emits one, and it is the only marker that reliably separates one
#: item from the next once ``extract_text`` has flattened the columns.
_POSITION_LINE = re.compile(r"^\s*(\d{1,3})\s*$")

#: Where the item table stops. Reached the totals block, so there are no more
#: items, whatever else is below.
_TOTALS_BOUNDARY = re.compile(
    r"(?:subtotal|zwischensumme|subtotaal|net\s*total|gesamtbetrag|total|totaal|mwst|btw|vat)",
    re.IGNORECASE,
)

#: A unit of measure: a short alphabetic token sitting between the quantity and
#: the price. Matched so it can be excluded from the description rather than
#: captured -- nothing downstream compares units yet, and pretending otherwise
#: would put an unchecked field in front of a reviewer.
_UNIT_TOKEN = re.compile(r"^[A-Za-z]{1,8}\.?$")


def _numeric_lines(block: list[str]) -> list[int]:
    """Indices of lines that parse as a number, in order."""
    return [
        index
        for index, line in enumerate(block)
        if _AMOUNT_PATTERN.fullmatch(line.strip()) and _parse_amount(line) is not None
    ]


def _find_line_items(lines: list[str]) -> tuple[ExtractedLineItem, ...]:
    """Read the item table.

    ``extract_text`` flattens a table into one line per cell, in reading order,
    so a record arrives as::

        01
        NG-0O10l
        Hex bolt M8x40 zinc plated, DIN 933, boxed
        100
        12,00
        BOX
        18,40
        220,80

    Two things make that parseable without knowing the column geometry. The
    position number is alone on its line and starts a record. And the last three
    numbers in a record are always quantity, unit price and line total, in that
    order, because they are the last three columns.

    Reading the numbers from the *end* is what makes the wrapped description
    safe. "boxed 100" wraps, so ``100`` appears as a numeric line in the middle
    of the record -- a parser taking the first three numbers would read a
    quantity of 100 and a unit price of 12. Taking the last three leaves the
    stray number where it belongs, in the description.

    Anything that does not fit this shape yields no line items rather than
    guessed ones. A missing line is a check the matcher records as skipped; an
    invented line is a price comparison against a number nobody printed.
    """
    # A bare number on its own line is not enough to identify a position: this
    # layout wraps "…boxed 100" and the stray "100" looks exactly like one, so
    # every record was being split in half by its own description.
    #
    # What separates them is that positions count. Keeping only the candidates
    # that form a run 1, 2, 3… discards the wrapped fragment without needing to
    # know anything about the column geometry.
    candidates = [
        (index, int(match.group(1)))
        for index, line in enumerate(lines)
        if (match := _POSITION_LINE.match(line)) and not _TOTALS_BOUNDARY.search(line)
    ]
    starts: list[int] = []
    expected = 1
    for index, value in candidates:
        if value == expected:
            starts.append(index)
            expected += 1
    if not starts:
        return ()

    stop = next(
        (
            index
            for index, line in enumerate(lines)
            if index > starts[0] and _TOTALS_BOUNDARY.search(line)
        ),
        len(lines),
    )
    starts = [index for index in starts if index < stop]

    items: list[ExtractedLineItem] = []
    for order, start in enumerate(starts):
        end = starts[order + 1] if order + 1 < len(starts) else stop
        block = [line.strip() for line in lines[start + 1 : end] if line.strip()]
        if len(block) < 4:
            continue

        sku, rest = block[0], block[1:]
        numeric = _numeric_lines(rest)
        if len(numeric) < 3:
            continue

        qty_at, price_at, total_at = numeric[-3], numeric[-2], numeric[-1]
        quantity = _parse_amount(rest[qty_at])
        unit_price = _parse_amount(rest[price_at])
        line_total = _parse_amount(rest[total_at])
        if quantity is None or unit_price is None or line_total is None:
            continue

        description = " ".join(
            part
            for index, part in enumerate(rest[:qty_at])
            if not (index == qty_at - 1 and _UNIT_TOKEN.match(part))
        ).strip()
        if not description:
            continue

        # The same corroboration the totals get: three numbers that agree are
        # three independent readings confirming each other, and three that do
        # not are a warning that the columns were misread.
        reconciles = abs(quantity * unit_price - line_total) <= Decimal("0.01")
        confidence = 0.88 if reconciles else 0.5

        items.append(
            ExtractedLineItem(
                description=ExtractedField(
                    value=description, confidence=confidence, raw_text=description
                ),
                quantity=ExtractedField(value=quantity, confidence=confidence),
                unit_price=ExtractedField(value=unit_price, confidence=confidence),
                line_total=ExtractedField(value=line_total, confidence=confidence),
                sku=(
                    ExtractedField(value=sku, confidence=confidence, raw_text=sku)
                    if _TOKEN_PATTERN.fullmatch(sku)
                    else None
                ),
            )
        )

    return tuple(items)


# ------------------------------------------------------- payment details ---
#
# The last four digits of the account and of the routing code, which is all any
# of these layouts prints. Everything ahead of them is masked on the page, so
# there is no fuller value to read.
#
# `blz` and `bankcode` are both mandatory. The continental layout picks its
# routing label from the vendor's tax-id country -- BLZ for German invoices,
# Bankcode for Dutch ones -- so a BLZ-only pattern passes the German fixture and
# silently reads nothing from every Netherlands vendor. That is the failure this
# whole feature exists to prevent, one level down.
#
# There is no bare `account` alternative on purpose: it would match
# "Account Manager: ext 4417" and hand four digits of a phone extension to the
# highest-severity code in the system.
_BANK_ACCOUNT_LABELS = r"(?:iban|account\s*(?:number|no\.?|ending)|kontonummer|rekeningnummer)"
_BANK_ROUTING_LABELS = (
    r"(?:routing\s*/\s*(?:sort\s*code|aba)|routing\s*(?:number|no\.?|ending)?"
    r"|sort\s*code|bankleitzahl|bankcode|blz|aba)"
)

#: Longest alternative first: Python's alternation is leftmost-first, so
#: `routing/aba` has to be tried before `routing` or the split below leaves a
#: stray `/ABA` sitting in the value span.
_BANK_ANY_LABEL = re.compile(f"(?:{_BANK_ROUTING_LABELS}|{_BANK_ACCOUNT_LABELS})", re.IGNORECASE)


def _masked_span(text: str) -> str | None:
    """The four digits at the end of a masked value, or None.

    Two rules, both doing more work than they look like.

    *Reject anything containing a lowercase letter.* Every real value span is
    mask characters, uppercase and digits -- `DEXX XXXX XXXX XXXX XX82 91`,
    `XXXX 0248`, `....8291`, and four U+007F characters followed by `8291`.
    Prose is not. Enumerating mask codepoints instead would have missed that
    last one: the layout renders its bullets as U+007F, so a literal `[Xx•.]`
    class matches none of those fixtures while looking obviously correct.

    *Strip every separator, then require exactly four digits.* The continental
    layout groups a padded IBAN in fours, so where the last four digits break
    depends on the national IBAN length -- `XX82 91` for a 22-character German
    one, `X829 1` for a Swiss. Any offset-based rule is wrong for some country.
    Requiring exactly four also refuses a date (six or eight digits) and an
    amount, and refuses a span that greedily swallowed both values.
    """
    if any(char.islower() for char in text):
        return None
    digits = "".join(char for char in text if char.isdigit())
    return digits if len(digits) == 4 else None


def _find_masked_last4(lines: list[str], label_pattern: str) -> ExtractedField[str]:
    """Read one masked payment identifier.

    Deliberately not `_find_labelled_token`. That helper takes the first token
    after the label, which on these four layouts returns `DEXX`, `XXXX`, `ABA`
    and `ending` -- four confidently wrong readings out of eight, feeding the
    shortest clock in the system.
    """
    label_re = re.compile(label_pattern + r"(?![\w-])\s*[:#]?\s*", re.IGNORECASE)
    candidates: list[tuple[str, float, str]] = []

    for index, line in enumerate(lines):
        match = label_re.search(line)
        if match is None:
            continue

        # Two layouts print both identifiers on one line, so a span has to stop
        # at the next payment label or the routing digits bleed into the
        # account's value and each reads the other's.
        def span(text: str) -> str:
            following = _BANK_ANY_LABEL.search(text)
            return text[: following.start()] if following else text

        same_line = _masked_span(span(line[match.end() :]))
        if same_line is not None:
            candidates.append((same_line, 0.88, line[match.end() :].strip()))
            continue

        # Exactly the next line, never a forward scan. The continental bank box
        # flattens to `IBAN:`, value, `Waehrung:`, `EUR`, `BLZ:`, value -- a
        # scan looking for the next line with digits would read the currency.
        if index + 1 < len(lines):
            next_line = _masked_span(span(lines[index + 1]))
            if next_line is not None:
                candidates.append((next_line, 0.58, lines[index + 1].strip()))

    # The parsed digits are what compete, not the raw span: the same account
    # printed twice under different masks is one reading, and two genuinely
    # different last-four values is the ambiguity worth discounting.
    return _resolve_candidates(candidates)


def _find_labelled_amount(
    lines: list[str], label_pattern: str, currency: str | None
) -> ExtractedField[Decimal]:
    label_re = re.compile(label_pattern + r"(?![\w-])", re.IGNORECASE)
    candidates: list[tuple[Decimal, float, str]] = []

    for index, line in enumerate(lines):
        if not label_re.search(line):
            continue
        # The value is usually on the same line; some layouts put it on the next.
        for offset, base_confidence in ((0, 0.86), (1, 0.62)):
            if index + offset >= len(lines):
                continue
            target = lines[index + offset]
            if offset == 0:
                target = label_re.sub(" ", target)
            for match in _AMOUNT_PATTERN.finditer(target):
                # "zzgl. MwSt. 19,00 %" carries the rate on the label line and
                # the amount on the next one. Read naively the rate wins, being
                # on the same line and therefore more confident, and the invoice
                # comes out with 19.00 of tax instead of 58.29. A percentage is
                # never a money amount, whatever line it is on.
                if _PERCENT_SUFFIX.match(target[match.end() :]):
                    continue
                amount = _parse_amount(match.group())
                if amount is None:
                    continue
                confidence = base_confidence
                if currency and any(sym in target for sym in _CURRENCY_SYMBOLS):
                    confidence += 0.05
                candidates.append((amount, min(confidence, 0.95), match.group().strip()))

    return _resolve_candidates(candidates)


def _resolve_candidates[T](candidates: list[tuple[T, float, str]]) -> ExtractedField[T]:
    """Pick the strongest candidate and discount for ambiguity.

    Competition is signal. Three plausible readings of one field is a genuinely
    less certain result than one, and an extractor that reports the same
    confidence for both is not measuring anything.
    """
    if not candidates:
        return ExtractedField(value=None, confidence=0.0)

    candidates.sort(key=lambda c: c[1], reverse=True)
    best_value, best_confidence, raw = candidates[0]

    distinct = {c[0] for c in candidates}
    if len(distinct) > 1:
        best_confidence *= max(0.55, 1.0 - 0.15 * (len(distinct) - 1))

    return ExtractedField(value=best_value, confidence=round(best_confidence, 4), raw_text=raw)


def _corroborate_totals(
    subtotal: ExtractedField[Decimal],
    tax: ExtractedField[Decimal],
    total: ExtractedField[Decimal],
) -> tuple[ExtractedField[Decimal], ExtractedField[Decimal], ExtractedField[Decimal]]:
    """Raise all three when the arithmetic reconciles.

    Free evidence. If subtotal + tax equals total to the cent, three independent
    readings agree, and that is a stronger claim than any one of them alone.
    """
    if subtotal.value is None or tax.value is None or total.value is None:
        return subtotal, tax, total

    if abs((subtotal.value + tax.value) - total.value) <= Decimal("0.01"):

        def boost(field: ExtractedField[Decimal]) -> ExtractedField[Decimal]:
            return field.model_copy(update={"confidence": min(0.97, field.confidence + 0.10)})

        return boost(subtotal), boost(tax), boost(total)

    # Disagreement is also evidence — of a mis-read somewhere in the trio.
    def penalise(field: ExtractedField[Decimal]) -> ExtractedField[Decimal]:
        return field.model_copy(update={"confidence": field.confidence * 0.7})

    return penalise(subtotal), penalise(tax), penalise(total)


# -------------------------------------------------------------------- tokens --

_TOKEN_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9\-/_]{2,}", re.IGNORECASE)


def _find_labelled_token(lines: list[str], label_pattern: str) -> ExtractedField[str]:
    # ``(?![\w-])``: a label is only a label when something separates it from
    # the value. Without it "PO-44718" parses as the label "PO" plus the value
    # "-44718", the token finder drops the hyphen, and the purchase order
    # number silently loses its prefix -- which then matches no order at all.
    label_re = re.compile(label_pattern + r"(?![\w-])\s*[:#]?\s*", re.IGNORECASE)
    candidates: list[tuple[str, float, str]] = []

    for index, line in enumerate(lines):
        match = label_re.search(line)
        if not match:
            continue
        tail = line[match.end() :]
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        for text, confidence in ((tail, 0.88), (next_line, 0.58)):
            token = _TOKEN_PATTERN.search(text)
            if token:
                candidates.append((token.group().strip(), confidence, token.group()))
                break

    return _resolve_candidates(candidates)


def _find_labelled_date(lines: list[str], label_pattern: str) -> ExtractedField[str]:
    """Returns ISO-8601 text. Ambiguous formats are reported less confidently."""
    label_re = re.compile(label_pattern + r"\s*[:]?\s*", re.IGNORECASE)
    patterns: list[tuple[re.Pattern[str], str, float]] = [
        (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "ymd", 0.94),
        (re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})"), "dmy", 0.88),
        (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), "ambiguous", 0.55),
    ]
    candidates: list[tuple[str, float, str]] = []

    for index, line in enumerate(lines):
        match = label_re.search(line)
        if not match:
            continue
        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        for text, proximity in ((line[match.end() :], 1.0), (next_line, 0.7)):
            for pattern, order, confidence in patterns:
                found = pattern.search(text)
                if not found:
                    continue
                iso = _to_iso(found.groups(), order)
                if iso:
                    candidates.append((iso, confidence * proximity, found.group()))
            if candidates:
                break

    return _resolve_candidates(candidates)


def _to_iso(groups: tuple[str, ...], order: str) -> str | None:
    try:
        if order == "ymd":
            y, m, d = int(groups[0]), int(groups[1]), int(groups[2])
        elif order == "dmy":
            d, m, y = int(groups[0]), int(groups[1]), int(groups[2])
        else:
            # Genuinely undecidable without knowing the origin. US order is the
            # more common convention in this corpus and the confidence already
            # reflects the uncertainty rather than hiding it.
            m, d, y = int(groups[0]), int(groups[1]), int(groups[2])
        return date(y, m, d).isoformat()
    except (ValueError, IndexError):
        return None


def _find_currency(haystack: str) -> tuple[str | None, float]:
    for code in _CURRENCY_CODES:
        if re.search(rf"\b{code}\b", haystack):
            return code, 0.93
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in haystack:
            # A symbol is weaker evidence than a code: "$" is at least five
            # different currencies.
            return code, 0.68
    return None, 0.0


def _find_vendor_name(lines: list[str]) -> ExtractedField[str]:
    """The hardest field for this tier, and it says so.

    A vendor name has no label and no format. The heuristic — a substantial line
    near the top that is not an address, a date or a number — is genuinely weak,
    so it reports genuinely low confidence and routes to a human. This is the
    field where the model tier will earn its keep, and having an honest floor is
    what will make that improvement measurable.
    """
    noise = re.compile(r"invoice|rechnung|factuur|date|page|tel|fax|www|@|^\d", re.IGNORECASE)
    for line in lines[:8]:
        if len(line) < 4 or len(line) > 80 or noise.search(line):
            continue
        if sum(ch.isdigit() for ch in line) > len(line) * 0.3:
            continue
        return ExtractedField(value=line.strip(), confidence=0.45, raw_text=line)
    return ExtractedField(value=None, confidence=0.0)


def _classify_document(
    haystack: str, total: Decimal | None
) -> tuple[Literal["invoice", "credit_note", "unknown"], float]:
    lowered = haystack.lower()
    if "credit note" in lowered or "gutschrift" in lowered or "creditnota" in lowered:
        return "credit_note", 0.92
    if total is not None and total < 0:
        return "credit_note", 0.80
    if "invoice" in lowered or "rechnung" in lowered or "factuur" in lowered:
        return "invoice", 0.90
    return "unknown", 0.30


def _empty_result(started: float, *, reason: str) -> ExtractionResult:
    """Zero confidence everywhere. Routes to a human by construction."""
    nothing_str: ExtractedField[str] = ExtractedField(value=None, confidence=0.0)
    nothing_dec: ExtractedField[Decimal] = ExtractedField(value=None, confidence=0.0)
    return ExtractionResult(
        invoice=ExtractedInvoice(
            document_type=ExtractedField(value="unknown", confidence=0.0, raw_text=reason),
            invoice_number=nothing_str,
            vendor_name=nothing_str,
            vendor_tax_id=nothing_str,
            # Explicit, not left to the default. A document with no text
            # layer WAS asked for its payment details by this version and
            # yielded none; that is a different fact from a reading written
            # before the extractor knew to ask.
            bank_account_last4=nothing_str,
            bank_routing_last4=nothing_str,
            po_number=nothing_str,
            issue_date=nothing_str,
            due_date=nothing_str,
            currency=nothing_str,
            subtotal=nothing_dec,
            tax_amount=nothing_dec,
            total=nothing_dec,
        ),
        extractor_kind=ExtractorKind.DETERMINISTIC,
        extractor_id=EXTRACTOR_VERSION,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


__all__ = ["EXTRACTOR_VERSION", "DeterministicExtractor", "ExtractedLineItem"]
