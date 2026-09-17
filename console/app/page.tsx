import { redirect } from "next/navigation";

import { readSession } from "@/lib/session";

/**
 * The front door.
 *
 * This page used to redirect straight to Keycloak, which meant the first thing
 * anyone ever saw of this project was a login form belonging to a different
 * product, with no statement of what they were logging in to. That is a bad
 * front door for an internal tool and a worse one for something a stranger is
 * asked to evaluate in thirty seconds.
 *
 * So: signed in, go to the queue — the queue is the product and there is no
 * reason to make somebody click past a brochure to reach their work. Signed
 * out, explain the thing, then offer the door.
 *
 * Deliberately not a marketing page. No claimed percentages, no invented
 * customers, no throughput numbers — this system has never processed a real
 * invoice, and claiming otherwise would be the one unforgivable thing on a page
 * whose whole subject is a system that refuses to guess.
 */

export const dynamic = "force-dynamic";

const STEPS: { title: string; detail: string }[] = [
  {
    title: "It arrives",
    detail:
      "A PDF is uploaded and stored under a hash of its own contents, so the same document sent twice is the same document — not two.",
  },
  {
    title: "It gets read",
    detail:
      "Vendor, invoice number, line items, totals. Every field carries a confidence score, and a field read badly is treated as a field not read.",
  },
  {
    title: "It gets checked three ways",
    detail:
      "Against the purchase order — what we agreed to buy — and the goods receipt — what actually turned up. Line by line, not just on the total.",
  },
  {
    title: "It gets decided",
    detail:
      "If everything agrees, the system approves it itself and records that it did. If anything disagrees, it raises an exception naming what was wrong.",
  },
  {
    title: "It gets routed",
    detail:
      "Each exception goes to whoever can resolve that kind of problem, with a deadline attached. A disputed price is the buyer's. Changed bank details are treasury's, within four hours.",
  },
  {
    title: "It gets paid",
    detail:
      "Once approved, once — with the money recorded in a double-entry ledger the database will not let you unbalance.",
  },
];

const PROPERTIES: { title: string; detail: string }[] = [
  {
    title: "It refuses to guess",
    detail:
      "A total it could not read is recorded as absent, never as zero. A check that could not run is recorded as not having run — because a check that quietly did not happen looks exactly like one that passed.",
  },
  {
    title: "No model output can move money",
    detail:
      "The triage agent has four read-only tools and no way to write. Its database session is opened read-only, so an attempt to write fails inside Postgres rather than at a guardrail somebody could remove.",
  },
  {
    title: "The books cannot silently stop adding up",
    detail:
      "Every movement of money is a set of entries summing to zero, enforced by the database at commit time — including against an edit or deletion that would unbalance an entry already written.",
  },
  {
    title: "Claims are tested, not asserted",
    detail:
      "The security properties are verified by deleting them and confirming the suite fails. A test that still passes against a broken system is worse than no test.",
  },
];

const ACCOUNTS: { user: string; sees: string }[] = [
  { user: "buyer", sees: "Price and quantity disputes, closed purchase orders" },
  { user: "clerk", sees: "Duplicates, arithmetic errors, unknown vendors" },
  { user: "treasury", sees: "Changed bank details — the four-hour queue" },
  { user: "receiving", sees: "Invoices for goods that never arrived" },
  { user: "auditor", sees: "Everything, and can decide nothing" },
];

export default async function Home() {
  const session = await readSession();
  if (session) redirect("/queue");

  return (
    <>
      <h2>Invoices, checked before they are paid</h2>
      <p className="lede">
        Threshold decides which supplier invoices can be paid automatically, and puts the rest in
        front of the one person who can resolve them. Every screen here is backed by a real decision
        recorded in a real database — nothing on it is a mock.
      </p>

      <div className="card">
        <h3>The problem it solves</h3>
        <p>
          Before a company pays an invoice, somebody has to check it against what was ordered and
          what actually arrived. Done by hand that is slow, and the things it misses are expensive:
          the same invoice paid twice, a unit price that crept up between the order and the bill, or
          a supplier&apos;s bank details changed by somebody who is not the supplier.
        </p>
        <p className="meta">
          That last one is the reason this exists. It is the most profitable invoice fraud there is,
          and it arrives looking like an ordinary invoice from a vendor you really do owe money to.
        </p>
      </div>

      <h3>Follow one invoice through</h3>
      <ol className="steps">
        {STEPS.map((step) => (
          <li key={step.title}>
            <strong>{step.title}</strong>
            <p>{step.detail}</p>
          </li>
        ))}
      </ol>

      <h3>What is unusual about it</h3>
      {PROPERTIES.map((property) => (
        <div className="card" key={property.title}>
          <strong>{property.title}</strong>
          <p>{property.detail}</p>
        </div>
      ))}

      <h3>Have a look</h3>
      <p>
        Six demo accounts, one per role, each with the password the same as the username. Which
        invoices you can act on depends on which exceptions you own — that is the authorisation
        rule rather than a display filter, and you can watch it refuse you.
      </p>
      <table>
        <thead>
          <tr>
            <th>Sign in as</th>
            <th>Handles</th>
          </tr>
        </thead>
        <tbody>
          {ACCOUNTS.map((account) => (
            <tr key={account.user}>
              <td>
                <code>{account.user}</code>
              </td>
              <td>{account.sees}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="actions">
        <a className="primary" href="/api/auth/login">
          Sign in to the console
        </a>
      </p>
      <p className="meta">
        Start as <code>buyer</code> — it owns price disputes, which is what most of the blocked
        invoices in this demo are.
      </p>
    </>
  );
}
