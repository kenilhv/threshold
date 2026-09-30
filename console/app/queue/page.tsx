import { redirect } from "next/navigation";

import { Masthead } from "@/components/masthead";
import { ApiError, getQueue, type QueueItem } from "@/lib/api";
import { exceptionName, money, roleName, stamp, tally, until } from "@/lib/format";
import { readSession } from "@/lib/session";

/**
 * The queue is the product.
 *
 * What a reviewer needs to triage in seconds: what is wrong, whose it is, when
 * it is due, and whether they can act. Nothing else. There is no touchless-rate
 * figure and no chart, because neither is computable from anything but seeded
 * data, and a number that cannot be true is worse than no number.
 *
 * Printed as two pages of one run: what this reader can decide, then what is
 * waiting on somebody else. Both keep the API's order, soonest deadline first.
 *
 * Rendered on the server. There is no client-side state here at all — the queue
 * changes when somebody decides something, and that is a navigation.
 */

export const dynamic = "force-dynamic";

function Row({ item }: { item: QueueItem }) {
  const due = item.due_at ? until(item.due_at) : null;
  const blocked = item.disposition === "blocked";
  return (
    <tr>
      <td data-label="Status">
        {/* The word carries it; the red ribbon only repeats it. */}
        <span className={`status${blocked ? " red" : ""}`}>{blocked ? "Blocked" : "Review"}</span>
      </td>
      {/* Who and how much, first and together. A queue is scanned in that
          order before a word of it is read. */}
      <td data-label="Vendor" className="vendor">
        <a href={`/invoice/${item.document_id}`}>{item.vendor ?? "Vendor did not resolve"}</a>
      </td>
      <td data-label="Amount" className="num">
        {item.amount ? (
          <>
            {money(item.amount)}
            <span className="unit">{item.currency}</span>
          </>
        ) : (
          <span className="red">unreadable</span>
        )}
      </td>
      <td data-label="Why it stopped">
        {tally(item.exception_codes).map(({ code, count }) => (
          <span className="why" key={code}>
            {exceptionName(code)}
            {count > 1 && <span className="print muted"> · {count} lines</span>}
          </span>
        ))}
      </td>
      <td data-label="Owner">{item.owners.map(roleName).join(", ")}</td>
      <td data-label="Due" className="print due">
        {due ? (
          <>
            <span className={due.overdue ? "red" : undefined}>{due.text}</span>
            <span className="sub">{stamp(item.due_at as string)} UTC</span>
          </>
        ) : (
          "—"
        )}
      </td>
      <td data-label="Decision">
        {item.can_decide ? (
          <a href={`/invoice/${item.document_id}`}>
            Review<span className="sr"> {item.vendor ?? "invoice"}</span>
          </a>
        ) : (
          /* Not a disabled button. A control that looks like a control and
             does nothing is worse than its absence; the reason is what the
             reviewer actually needs. */
          <span className="not-yours">Not yours to decide</span>
        )}
      </td>
    </tr>
  );
}

function Section({ title, items }: { title: string; items: QueueItem[] }) {
  if (items.length === 0) return null;
  return (
    <tbody>
      <tr className="section">
        <th colSpan={7} scope="colgroup">
          {title}
          <span className="count">{items.length}</span>
        </th>
      </tr>
      {items.map((item) => (
        <Row item={item} key={item.match_result_id} />
      ))}
    </tbody>
  );
}

export default async function QueuePage({
  searchParams,
}: {
  searchParams: Promise<{ mine?: string }>;
}) {
  const session = await readSession();
  if (!session) redirect("/api/auth/login?return=/queue");

  const params = await searchParams;
  const mine = params.mine === "true";

  let items: QueueItem[] = [];
  try {
    items = await getQueue(mine);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/api/auth/login?return=/queue");
    throw error;
  }

  const yours = items.filter((i) => i.can_decide);
  const others = items.filter((i) => !i.can_decide);

  return (
    <>
      <Masthead current="queue" />
      <main id="main">
        <div className="report-head">
          <h1>Waiting for a person</h1>
          <p className="run print" aria-live="polite">
            <span>
              <b>{items.length}</b> waiting
            </span>
            <span>
              <b>{yours.length}</b> yours to decide
            </span>
            <span className="filter">
              <a href="/queue" aria-current={!mine ? "true" : undefined}>
                Everything
              </a>
              <a href="/queue?mine=true" aria-current={mine ? "true" : undefined}>
                Only mine
              </a>
            </span>
          </p>
        </div>
        <p className="lede">
          Every invoice here raised at least one exception the rules would not clear on their own.
          Invoices that raised nothing are not listed: the system already decided those, and a queue
          containing work nobody has to do teaches people to skim it.
        </p>

        {items.length === 0 ? (
          <p className="empty">
            Nothing is waiting.{" "}
            {mine
              ? "Nothing here is yours to decide; the full queue may still hold work for other desks."
              : "Every invoice that arrived has been decided, by the system or by a person."}
          </p>
        ) : (
          <table className="queue stack">
            <caption className="sr">Invoices awaiting a decision, soonest deadline first</caption>
            <thead>
              <tr>
                <th scope="col">Status</th>
                <th scope="col">Vendor</th>
                <th scope="col" className="num">
                  Amount
                </th>
                <th scope="col">Why it stopped</th>
                <th scope="col">Owner</th>
                <th scope="col">Due</th>
                <th scope="col">Decision</th>
              </tr>
            </thead>
            <Section title="Yours to decide" items={yours} />
            <Section title="Waiting on another desk" items={others} />
            <tfoot>
              <tr className="end-of-run">
                <td colSpan={7}>
                  <span>End of run</span> · <span>{items.length} waiting</span> ·{" "}
                  <span>{yours.length} yours to decide</span>
                </td>
              </tr>
            </tfoot>
          </table>
        )}
      </main>
    </>
  );
}
