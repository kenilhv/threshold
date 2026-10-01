import { redirect } from "next/navigation";

import { DecideForm } from "@/components/decide-form";
import { Masthead } from "@/components/masthead";
import {
  ApiError,
  getDecisions,
  getMatch,
  type DecisionView,
  type ExceptionView,
  type LineEvidence,
  type MatchView,
} from "@/lib/api";
import { exceptionName, money, quantity, roleName, roleNoun, stamp, until } from "@/lib/format";
import { readSession } from "@/lib/session";

/**
 * One invoice, its evidence, and the decision if this person may make it.
 *
 * `skipped_checks` is rendered at the same weight as the exceptions, and that
 * is the whole argument of this screen. A clean result only means something if
 * every check ran; an absent check and a passing check look identical from the
 * outside, and the difference is the entire value of the answer.
 *
 * The line table is the evidence the verdict came from: ordered, received and
 * billed side by side, so a reviewer checks the matcher instead of trusting it.
 *
 * The agent's escalation is rendered last and visibly apart, attributed to the
 * tier that wrote it and quoted rather than stated. Everything above it is
 * something this system established; that block is something a model said after
 * reading a document the supplier wrote. A screen that presented both in the
 * same voice would be the last place an injected instruction has to get to.
 */

export const dynamic = "force-dynamic";

/**
 * The three outcomes, said plainly.
 *
 * `blocked` is the one worth spelling out: it does not mean the invoice is
 * fraudulent or that anything is broken. It means the rules found a
 * disagreement they are not allowed to resolve on their own, which is the
 * system working rather than failing.
 */
const DISPOSITIONS: Record<string, { label: string; says: string }> = {
  blocked: {
    label: "Blocked",
    says: "The rules found something they will not clear on their own. Somebody who owns the problem has to decide. Nothing is wrong with the system — this is it working.",
  },
  needs_review: {
    label: "Needs review",
    says: "Nothing here stops the payment outright, but something is worth a person's eyes before it goes.",
  },
  auto_approve: {
    label: "Approved by the system",
    says: "Every check agreed, so the system approved this itself and recorded that it did. No person was involved.",
  },
};

const SEVERITY: Record<string, string> = {
  block: "Blocks payment",
  review: "Needs a look",
  info: "For information",
};

/** Where the money is, in the order it goes there. */
const STATIONS: { state: string; label: string; says: string }[] = [
  { state: "pending", label: "Authorised", says: "not yet sent" },
  { state: "submitting", label: "Sent to the rail", says: "accounted for, awaiting an answer" },
  { state: "submitted", label: "Accepted by the rail", says: "in transit" },
  { state: "settled", label: "Settled", says: "confirmed by the rail" },
];

function ownersOf(match: MatchView): string[] {
  return [...new Set(match.exceptions.map((e) => e.owner_role))].sort();
}

function canDecide(match: MatchView, roles: string[]): boolean {
  const owners = ownersOf(match);
  if (owners.length === 0) return false;
  if (roles.includes("auditor")) return false;
  return owners.every((owner) => roles.includes(owner));
}

/** One finding per kind of exception, with the lines it was raised on. */
function grouped(exceptions: ExceptionView[]) {
  const groups = new Map<string, { first: ExceptionView; all: ExceptionView[] }>();
  for (const exception of exceptions) {
    const key = `${exception.code}|${exception.owner_role}`;
    const group = groups.get(key);
    if (group) group.all.push(exception);
    else groups.set(key, { first: exception, all: [exception] });
  }
  return [...groups.values()];
}

/**
 * The invoice line an exception is about. Price findings name the invoice line;
 * quantity findings are raised against the order line, because that is where
 * received quantities are counted, so those are found through the pairing.
 */
function lineOf(exception: ExceptionView, lines: LineEvidence[]): number | null {
  if (exception.invoice_line_number !== null) return exception.invoice_line_number;
  if (exception.purchase_order_line_number === null) return null;
  const paired = lines.find((l) => l.po_line === exception.purchase_order_line_number);
  return paired ? paired.invoice_line : null;
}

/** Which cells of a line the exceptions point at, and by how much. */
function flags(line: LineEvidence, match: MatchView) {
  const on = match.exceptions.filter((e) => lineOf(e, match.lines) === line.invoice_line);
  const price = on.find((e) => e.code === "price_variance");
  const variance = price?.breach?.variance;
  return {
    price: price !== undefined,
    priceOver: typeof variance === "string" ? money(variance) : null,
    quantity: on.some((e) => e.code === "quantity_variance" || e.code === "receipt_missing"),
    under: on.some((e) => e.code === "under_billed"),
    arithmetic: on.some((e) => e.code === "line_total_mismatch"),
  };
}

function figure(value: string | null, format: (v: string) => string) {
  return value === null ? <span className="red">not read</span> : format(value);
}

function LineTable({ match }: { match: MatchView }) {
  if (match.lines.length === 0) return null;
  return (
    <>
      <h2>Line by line</h2>
      <table className="lines stack">
        <caption className="sr">
          Each invoice line beside the order line it was paired with: ordered, received and billed
        </caption>
        <thead>
          <tr className="groups">
            <td colSpan={2} />
            <th colSpan={3} scope="colgroup">
              Quantity
            </th>
            <th colSpan={2} scope="colgroup">
              Unit price{match.currency ? `, ${match.currency}` : ""}
            </th>
            <td />
          </tr>
          <tr>
            <th scope="col">Line</th>
            <th scope="col">Description</th>
            <th scope="col" className="num">
              Ordered
            </th>
            <th scope="col" className="num">
              Received
            </th>
            <th scope="col" className="num">
              Billed
            </th>
            <th scope="col" className="num">
              Agreed
            </th>
            <th scope="col" className="num">
              Billed
            </th>
            <th scope="col" className="num">
              Line total
            </th>
          </tr>
        </thead>
        <tbody>
          {match.lines.map((line) => {
            const f = flags(line, match);
            const onOrder = line.po_line !== null;
            return (
              <tr key={line.invoice_line}>
                <td data-label="Line" className="print">
                  {line.invoice_line}
                  {onOrder && line.po_line !== line.invoice_line && (
                    <span className="sub">PO {line.po_line}</span>
                  )}
                </td>
                <td data-label="Description" className="desc">
                  {line.description || <span className="muted">no description read</span>}
                </td>
                <td data-label="Ordered" className="num">
                  {onOrder ? (
                    figure(line.ordered_quantity, quantity)
                  ) : (
                    <span className="red">not on the order</span>
                  )}
                </td>
                <td data-label="Received" className="num">
                  {onOrder ? figure(line.received_quantity, quantity) : "—"}
                </td>
                <td data-label="Billed qty" className={`num${f.quantity ? " over" : ""}`}>
                  {figure(line.billed_quantity, quantity)}
                  {f.quantity && <span className="mark">more than arrived</span>}
                  {f.under && <span className="mark">less than arrived</span>}
                </td>
                <td data-label="Agreed price" className="num">
                  {onOrder ? figure(line.agreed_unit_price, money) : "—"}
                </td>
                <td data-label="Billed price" className={`num${f.price ? " over" : ""}`}>
                  {figure(line.billed_unit_price, money)}
                  {f.priceOver && <span className="mark">{f.priceOver} over</span>}
                </td>
                <td data-label="Line total" className={`num${f.arithmetic ? " over" : ""}`}>
                  {figure(line.billed_line_total, money)}
                  {f.arithmetic && <span className="mark">does not add up</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="note">
        Received counts every delivery booked against the order to date, so it can include one
        that arrived after this invoice was judged.
      </p>
    </>
  );
}

export default async function InvoicePage({ params }: { params: Promise<{ id: string }> }) {
  const session = await readSession();
  const { id } = await params;
  if (!session) redirect(`/api/auth/login?return=/invoice/${id}`);

  let match: MatchView;
  let history: DecisionView[];
  try {
    [match, history] = await Promise.all([getMatch(id), getDecisions(id)]);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      redirect(`/api/auth/login?return=/invoice/${id}`);
    }
    throw error;
  }

  const owners = ownersOf(match);
  const permitted = canDecide(match, session.roles);
  // The matcher's verdict never changes after a person decides; the decision
  // is a separate, later fact. Once there is one, it leads, so a settled
  // invoice does not still read "Blocked, due in 2 days".
  const final = history.find((d) => d.decision === "approved" || d.decision === "rejected");
  const settled = final !== undefined;
  const disposition = DISPOSITIONS[match.disposition] ?? {
    label: match.disposition.replace(/_/g, " "),
    says: "",
  };
  const payment = match.payment;
  const reached = payment ? STATIONS.findIndex((s) => s.state === payment.state) : -1;

  return (
    <>
      <Masthead current="queue" />
      <main id="main">
        <a className="back" href="/queue">
          Back to the queue
        </a>

        <div className="report-head invoice-head">
          <div>
            <h1>{match.vendor ? match.vendor.display_name : "Vendor did not resolve"}</h1>
            <dl className="facts print">
              {match.vendor ? (
                <>
                  <div>
                    <dt>Vendor</dt>
                    <dd>{match.vendor.vendor_number}</dd>
                  </div>
                  <div>
                    <dt>Country</dt>
                    <dd>{match.vendor.country_code}</dd>
                  </div>
                  <div>
                    <dt>Standing</dt>
                    <dd>{match.vendor.status}</dd>
                  </div>
                </>
              ) : (
                <div>
                  <dt>Vendor</dt>
                  <dd>nothing on this invoice matched a vendor on file</dd>
                </div>
              )}
              <div>
                <dt>Judged</dt>
                <dd>{stamp(match.decided_at)} UTC</dd>
              </div>
              <div>
                <dt>Under policy</dt>
                <dd>{match.policy_version}</dd>
              </div>
              <div>
                <dt>Evidence</dt>
                <dd>version {match.version}</dd>
              </div>
            </dl>
          </div>
          {/* The number the whole screen is about. Absent when the total could
              not be read, which is not a missing field -- it is the reason one
              of the exceptions below is there. */}
          <p className="total">
            {match.amount ? (
              <>
                {money(match.amount)}
                <span className="unit">{match.currency}</span>
              </>
            ) : (
              <span className="red">total not read</span>
            )}
          </p>
        </div>

        {/* What the status means, in words. A reviewer who has to learn the
            vocabulary before they can read the screen will guess instead, and
            the guess for "blocked" is usually "broken". */}
        {final ? (
          <div className="verdict">
            <span className="status">{final.decision}</span>
            <p>
              {`${disposition.label} by the rules, then ${final.decision} by ${
                final.actor_label ?? "the system"
              } on ${stamp(final.decided_at)} UTC. What stopped it is kept below as it was.`}
            </p>
          </div>
        ) : (
          <div className="verdict">
            <span className={`status${match.disposition === "blocked" ? " red" : ""}`}>
              {disposition.label}
            </span>
            <p>{disposition.says}</p>
          </div>
        )}

        <h2>Why it stopped</h2>
        <ul className="findings">
          {match.exceptions.length === 0 && match.skipped_checks.length === 0 && (
            <li>
              <span>No exceptions were raised, and every check ran.</span>
            </li>
          )}
          {grouped(match.exceptions).map(({ first, all }) => {
            const lines = all
              .map((e) => lineOf(e, match.lines))
              .filter((n): n is number => n !== null);
            const due = until(all.map((e) => e.due_at).sort()[0] ?? first.due_at);
            return (
              <li key={`${first.code}-${first.owner_role}`}>
                <div>
                  <strong>{exceptionName(first.code)}</strong>
                  <span className="print muted">
                    {" · "}
                    {first.code}
                    {lines.length > 0 &&
                      ` · ${lines.length === 1 ? "line" : "lines"} ${lines.join(", ")}`}
                  </span>
                </div>
                <div className="route print">
                  {settled ? (
                    `${roleName(first.owner_role)} · decided`
                  ) : (
                    <>
                      {SEVERITY[first.severity] ?? first.severity} · {roleName(first.owner_role)} ·{" "}
                      <span className={due.overdue ? "red" : undefined}>{due.text}</span>
                    </>
                  )}
                </div>
                {/* Line-level findings are in the table below, with their
                    numbers. Anything about the document as a whole is said
                    here, in the matcher's words. */}
                {/* Every one, not the first: two document-level findings of
                    one kind say different things, and the second sentence is
                    as much evidence as the first. */}
                {lines.length === 0 &&
                  all.map((e, index) => (
                    <p className="said" key={index}>
                      {e.detail}
                    </p>
                  ))}
              </li>
            );
          })}
          {/* Same weight as the exceptions, deliberately. A check that did not
              run is not the same as a check that passed. */}
          {match.skipped_checks.map((skipped) => (
            <li key={skipped}>
              <div>
                <strong>Not checked</strong>
              </div>
              <div className="route print">counts against a clean result</div>
              <p className="said">{skipped}</p>
            </li>
          ))}
        </ul>

        <LineTable match={match} />

        {/* The money, if it has started moving. Above the agent's opinion
            because it is a fact about this invoice rather than a suggestion
            about it, and a reviewer scanning downward should meet the facts
            first. */}
        {payment && (
          <>
            <h2>
              Payment <span className="print muted">{money(payment.amount)}</span>
              <span className="unit">{payment.currency}</span>
            </h2>
            <ol className="stations">
              {STATIONS.map((station, index) => {
                const failedHere = payment.state === "failed" && index === STATIONS.length - 1;
                const current = station.state === payment.state || failedHere;
                return (
                  <li
                    key={station.state}
                    className={failedHere ? "failed" : undefined}
                    aria-current={current ? "step" : undefined}
                  >
                    {failedHere ? "Refused by the rail" : station.label}
                    <span className="sub print">
                      {failedHere
                        ? "the liability was posted back"
                        : index < reached || payment.state === "settled"
                          ? "done"
                          : current
                            ? station.says
                            : "not yet"}
                    </span>
                  </li>
                );
              })}
            </ol>
            {payment.failure_reason && (
              <p className="note">Refused: {payment.failure_reason}</p>
            )}
            {payment.rail_attempts > 1 && (
              <p className="note">
                The rail was asked {payment.rail_attempts} times. Each retry sent the same
                idempotency key, so the money moved at most once.
              </p>
            )}
            <p className="note print">
              {payment.rail_id}
              {payment.rail_reference ? ` · ${payment.rail_reference}` : ""}
            </p>
          </>
        )}

        {history.length > 0 && (
          <>
            <h2>Decisions</h2>
            <p className="lede">Append-only. This is the whole history, not the current state.</p>
            <table className="stack">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Who</th>
                  <th scope="col">Decision</th>
                  <th scope="col">Why</th>
                </tr>
              </thead>
              <tbody>
                {history.map((decision) => (
                  <tr key={decision.id}>
                    <td data-label="When" className="print">
                      {stamp(decision.decided_at)} UTC
                    </td>
                    <td data-label="Who">{decision.actor_label ?? "the system"}</td>
                    <td data-label="Decision" className="status">
                      {decision.decision.replace(/_/g, " ")}
                    </td>
                    <td data-label="Why">
                      {decision.reason ?? <span className="muted">no reason required</span>}
                      {decision.overrode.length > 0 && (
                        <span className="sub print">cleared: {decision.overrode.join(", ")}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {/* Rendered after the evidence and visibly apart from it. The
            exceptions above are things the system established; this is
            something an agent said, and a reviewer must never have to guess
            which they are reading.

            The attribution line is not decoration. The summary was written by
            a model that read this supplier's own line descriptions, so on a
            hostile invoice it is the supplier talking at one remove. Naming the
            author is what keeps that legible. */}
        {match.escalation && (
          <section className="agent" aria-labelledby="agent-heading">
            <h2 id="agent-heading">
              Suggested by <span className="print">{match.escalation.triager_id}</span>
            </h2>
            <p>
              An opinion about what to look at first. It changes nothing above: every exception
              keeps the owner and the deadline it was raised with.
            </p>
            <p>
              <b>{roleName(match.escalation.owner_role)} first</b>
              {" — "}
              {match.escalation.codes.map(exceptionName).join("; ")}
            </p>
            <blockquote>{match.escalation.summary}</blockquote>
            {match.escalation.uncited_codes.length > 0 && (
              <p>
                Did not mention: {match.escalation.uncited_codes.map(exceptionName).join("; ")}.
                Those exceptions are still open and still owned.
              </p>
            )}
          </section>
        )}

        {settled ? (
          <div className="refusal">
            <span className="status">Decided</span>
            <p>
              <strong>This invoice has been decided.</strong> There is no undo.
            </p>
          </div>
        ) : permitted ? (
          <DecideForm
            matchResultId={match.match_result_id}
            documentId={match.document_id}
            seenVersion={match.version}
            clearing={match.exceptions.map((e) => e.code)}
          />
        ) : (
          /* The missing button, with the reason where the button would be. */
          <div className="refusal">
            {/* The same words the queue prints for this row. */}
            <span className="status">Not yours to decide</span>
            <strong>You cannot decide this invoice.</strong>
            <p>
              {owners.length === 0
                ? "It raised no exceptions, so there is nothing to decide."
                : session.roles.includes("auditor")
                  ? "An auditor may read everything and decide nothing."
                  : `It raised exceptions owned by ${owners.map(roleNoun).join(" and ")}. You hold ${
                      session.roles.map(roleNoun).join(", ") || "no roles"
                    }.`}
            </p>
          </div>
        )}
      </main>
    </>
  );
}
