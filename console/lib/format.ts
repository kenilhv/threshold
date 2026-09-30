/**
 * Display formatting, and nothing else.
 *
 * The API sends money and quantities as decimal strings, exactly as stored.
 * Nothing here turns one into a float: grouping and trimming happen on the
 * digits, so what a reviewer reads is what was stored, only easier to read.
 * "18.4000" becomes "18.40" and never "18.399999999999999".
 */

const MINUS = "−";

/** Group the whole part and trim trailing zeros, keeping at least `minFraction` places. */
export function decimal(value: string, minFraction = 0): string {
  const negative = value.startsWith("-");
  const [whole = "0", fraction = ""] = value.replace(/^-/, "").split(".");
  let places = fraction.replace(/0+$/, "");
  if (places.length < minFraction) places = places.padEnd(minFraction, "0");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? MINUS : ""}${grouped}${places ? `.${places}` : ""}`;
}

/** A money amount to two places at least; a unit price keeps a fourth place if it has one. */
export function money(value: string): string {
  return decimal(value, 2);
}

/** A quantity to as many places as it actually has. */
export function quantity(value: string): string {
  return decimal(value, 0);
}

/** An amount without its sign, for columns where the side is printed separately. */
export function magnitude(value: string): string {
  return money(value.replace(/^-/, ""));
}

/**
 * How long until a deadline, in the unit a person plans in.
 *
 * Hours were the old unit everywhere, which is how a twelve-day-old item came
 * to read "overdue by 290h": true, and useless for deciding what to do first.
 */
export function until(iso: string, now: number = Date.now()): { text: string; overdue: boolean } {
  const minutes = Math.round((new Date(iso).getTime() - now) / 60_000);
  const overdue = minutes < 0;
  const span = Math.abs(minutes);
  let text: string;
  if (span < 60) text = `${span} min`;
  else if (span < 36 * 60) text = `${Math.round(span / 60)} h`;
  else text = `${Math.round(span / (24 * 60))} days`;
  return { text: overdue ? `${text} overdue` : `in ${text}`, overdue };
}

/** A fixed, unambiguous timestamp: "2 Oct 07:52". Server-rendered, so UTC is stated. */
export function stamp(iso: string): string {
  const at = new Date(iso);
  const day = at.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  const time = at.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  });
  return `${day} ${time}`;
}

/**
 * Every exception code, said the way the person who owns it would say it.
 *
 * The taxonomy is closed and frozen (`domain/taxonomy.py`), so this table is
 * complete; an unknown code falls back to its words rather than failing.
 */
const EXCEPTIONS: Record<string, string> = {
  no_po_reference: "No purchase order quoted",
  po_not_found: "Purchase order not on file",
  po_closed: "Purchase order already closed",
  vendor_unknown: "Vendor not on file",
  vendor_on_hold: "Vendor is on hold",
  vendor_mismatch: "Order belongs to another vendor",
  currency_mismatch: "Billed in a different currency",
  duplicate_suspected: "Possibly billed before",
  price_variance: "Billed above the agreed price",
  quantity_variance: "Billed for more than arrived",
  receipt_missing: "Nothing recorded as received",
  line_not_on_po: "Line is not on the order",
  under_billed: "Billed for less than arrived",
  arithmetic_mismatch: "Totals do not add up",
  line_total_mismatch: "A line total does not add up",
  low_confidence_field: "A field could not be read with confidence",
  checks_incomplete: "A check did not run",
  bank_details_changed: "Pay-to account has changed",
};

export function exceptionName(code: string): string {
  return EXCEPTIONS[code] ?? code.replace(/_/g, " ");
}

const ROLES: Record<string, string> = {
  ap_clerk: "AP clerk",
  buyer: "Buyer",
  receiving: "Receiving",
  treasury: "Treasury",
  controller: "Controller",
  auditor: "Auditor",
};

export function roleName(role: string): string {
  return ROLES[role] ?? role.replace(/_/g, " ");
}

/** The same, as a noun inside a sentence: "owned by buyer", "owned by AP clerk". */
export function roleNoun(role: string): string {
  const name = roleName(role);
  return name.startsWith("AP ") ? name : name.toLowerCase();
}

/** Codes repeat once per offending line; the queue wants each kind once, with a count. */
export function tally(codes: string[]): { code: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const code of codes) counts.set(code, (counts.get(code) ?? 0) + 1);
  return [...counts].map(([code, count]) => ({ code, count }));
}
