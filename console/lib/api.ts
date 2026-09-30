import { readSession } from "@/lib/session";

/**
 * The only place this console talks to the API.
 *
 * Every call is server-side and carries the session's access token. The browser
 * makes no cross-origin request and never sees a token; see `session.ts` for
 * why that trade is worth the extra hop.
 */

const API = process.env.THRESHOLD_API_URL ?? "http://api:8000";

export type QueueItem = {
  match_result_id: string;
  document_id: string;
  version: number;
  disposition: string;
  decided_at: string;
  due_at: string | null;
  vendor: string | null;
  amount: string | null;
  currency: string | null;
  exception_codes: string[];
  owners: string[];
  can_decide: boolean;
  blocked_reason: string | null;
};

export type ExceptionView = {
  code: string;
  severity: string;
  owner_role: string;
  detail: string;
  due_at: string;
  invoice_line_number: number | null;
  purchase_order_line_number: number | null;
  breach: Record<string, unknown> | null;
};

/**
 * What the triage agent said. Advisory, and rendered as such.
 *
 * `summary` is free text written by a model whose input included a document the
 * supplier controls. It is the one string on this screen that is neither a
 * system fact nor a person's words, and the screen has to say so -- a reviewer
 * who reads it as the system's own conclusion has been told something by the
 * supplier without knowing it.
 */
export type EscalationView = {
  owner_role: string;
  codes: string[];
  uncited_codes: string[];
  summary: string;
  triager_id: string;
  created_at: string;
};

/**
 * Where the money is, if it has started moving.
 *
 * `submitting` is the state worth understanding: the instruction is accounted
 * for and the rail has not answered. Not an error and not a success -- the
 * window in which a payment system either has a truthful record or does not.
 */
export type PaymentView = {
  payment_id: string;
  state: string;
  amount_minor: number;
  amount: string;
  currency: string;
  rail_id: string;
  rail_reference: string | null;
  failure_reason: string | null;
  rail_attempts: number;
  created_at: string;
  settled_at: string | null;
};

export type MatchView = {
  match_result_id: string;
  document_id: string;
  version: number;
  disposition: string;
  policy_version: string;
  decided_at: string;
  amount_minor: number | null;
  amount: string | null;
  currency: string | null;
  line_pairs: { invoice_line: number; po_line: number }[];
  skipped_checks: string[];
  exceptions: ExceptionView[];
  vendor: {
    vendor_number: string;
    display_name: string;
    country_code: string;
    status: string;
  } | null;
  escalation: EscalationView | null;
  payment: PaymentView | null;
  lines: LineEvidence[];
};

/**
 * One invoice line beside the order line it was paired with: the three
 * documents of the three-way match, as the numbers the matcher judged. Every
 * figure is the stored decimal as a string; `lib/format.ts` trims and groups
 * it and computes nothing. Null means not read, or not on the order.
 */
export type LineEvidence = {
  invoice_line: number;
  description: string;
  billed_quantity: string | null;
  billed_unit_price: string | null;
  billed_line_total: string | null;
  po_line: number | null;
  ordered_quantity: string | null;
  agreed_unit_price: string | null;
  received_quantity: string | null;
};

export type DecisionView = {
  id: string;
  decision: string;
  actor_label: string | null;
  reason: string | null;
  overrode: string[];
  decided_at: string;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: unknown,
  ) {
    super(`API responded ${status}`);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const session = await readSession();
  if (!session) throw new ApiError(401, { detail: "not signed in" });

  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      authorization: `Bearer ${session.accessToken}`,
      "content-type": "application/json",
    },
    // A queue is a live thing. Caching it would show a reviewer work somebody
    // else already decided, and the version check would then refuse them.
    cache: "no-store",
  });

  const text = await response.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) throw new ApiError(response.status, body);
  return body as T;
}

export function getQueue(mine: boolean): Promise<QueueItem[]> {
  return call<QueueItem[]>(`/v1/queue?mine=${mine ? "true" : "false"}`);
}

export function getMatch(documentId: string): Promise<MatchView> {
  return call<MatchView>(`/v1/documents/${documentId}/match`);
}

export function getDecisions(documentId: string): Promise<DecisionView[]> {
  return call<DecisionView[]>(`/v1/documents/${documentId}/decisions`);
}

export function postDecision(
  matchResultId: string,
  payload: { decision: string; reason: string; seen_version: number },
): Promise<DecisionView> {
  return call<DecisionView>(`/v1/match-results/${matchResultId}/decision`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * One account's standing balance.
 *
 * `kind` is carried because the sign alone does not say whether a balance is
 * healthy. Liabilities and equity run credit-negative, so accounts payable at
 * -482000 means we owe 4,820.00 and is exactly right, while the same number on
 * cash is an overdraft.
 */
export type AccountBalance = {
  account: string;
  kind: string;
  amount_minor: number;
  amount: string;
};

export type TrialBalance = {
  accounts: AccountBalance[];
  total_minor: number;
  balanced: boolean;
  currency: string;
};

export function getTrialBalance(): Promise<TrialBalance> {
  return call<TrialBalance>("/v1/ledger/trial-balance");
}
