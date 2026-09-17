import { redirect } from "next/navigation";

import { ApiError, getQueue, type QueueItem } from "@/lib/api";
import { readSession } from "@/lib/session";

/**
 * The queue is the product.
 *
 * What a reviewer needs to triage in seconds: what is wrong, whose it is, when
 * it is due, and whether they can act. Nothing else. There is no touchless-rate
 * tile and no chart, because neither is computable from anything but seeded
 * data, and a number that cannot be true is worse than no number.
 *
 * Rendered on the server. There is no client-side state here at all — the queue
 * changes when somebody decides something, and that is a navigation.
 */

export const dynamic = "force-dynamic";

function severityOf(item: QueueItem): "block" | "review" {
  return item.disposition === "blocked" ? "block" : "review";
}

function due(item: QueueItem): string {
  if (!item.due_at) return "—";
  const at = new Date(item.due_at);
  const hours = Math.round((at.getTime() - Date.now()) / 3_600_000);
  if (hours < 0) return `overdue by ${Math.abs(hours)}h`;
  return `in ${hours}h`;
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

  const actionable = items.filter((i) => i.can_decide).length;

  return (
    <>
      <h2>Waiting for a person</h2>
      <p className="lede">
        Every invoice here raised at least one exception the rules would not clear on their own.
        Invoices that raised nothing are not listed: the system already decided those, and a queue
        containing work nobody has to do teaches people to skim it.
      </p>

      <p className="meta" aria-live="polite">
        <span>
          {items.length} waiting · {actionable} you can decide
        </span>
        <span>
          {mine ? (
            <a href="/queue">Show everything</a>
          ) : (
            <a href="/queue?mine=true">Only mine</a>
          )}
        </span>
      </p>

      {items.length === 0 ? (
        <p className="empty">
          Nothing is waiting. {mine ? "Nothing here is yours to decide." : ""}
        </p>
      ) : (
        <table>
          <caption className="skip">
            Invoices awaiting a decision, soonest deadline first
          </caption>
          <thead>
            <tr>
              <th scope="col">Invoice</th>
              <th scope="col">Why it stopped</th>
              <th scope="col">Owner</th>
              <th scope="col">Due</th>
              <th scope="col">Decision</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.match_result_id}>
                {/* Who and how much, first and together. A queue is scanned in
                    that order before a word of it is read, and forty rows that
                    differ only in their exception codes cannot be triaged at
                    all -- which was true of this table until it had this
                    column. */}
                <td>
                  <a href={`/invoice/${item.document_id}`}>
                    {item.vendor ?? "Vendor did not resolve"}
                  </a>
                  <div className="meta">
                    {item.amount ? `${item.amount} ${item.currency}` : "amount unreadable"}
                  </div>
                </td>
                <td>
                  {item.exception_codes.map((c) => c.replace(/_/g, " ")).join(", ")}
                  <div className="meta">
                    <span className={`tag ${severityOf(item)}`}>
                      {item.disposition === "blocked" ? "Blocked" : "Needs review"}
                    </span>
                  </div>
                </td>
                <td>{item.owners.join(", ")}</td>
                <td>{due(item)}</td>
                <td>
                  {item.can_decide ? (
                    <a href={`/invoice/${item.document_id}`}>Review</a>
                  ) : (
                    /* Not a disabled button. A control that looks like a
                       control and does nothing is worse than its absence; the
                       reason is what the reviewer actually needs. */
                    <span className="meta">Not yours to decide</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
