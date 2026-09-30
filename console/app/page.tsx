import { redirect } from "next/navigation";

import { Masthead } from "@/components/masthead";
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
 * out, show the thing working, explain it, then offer the door.
 *
 * Deliberately not a marketing page. No claimed percentages, no invented
 * customers, no throughput numbers — this system has never processed a real
 * invoice, and claiming otherwise would be the one unforgivable thing on a page
 * whose whole subject is a system that refuses to guess.
 *
 * The specimen beside the headline is the demo's own over-billed invoice
 * (`scripts/demo_three_way_match.py`), printed the way the console prints it,
 * and labelled as synthetic where it stands.
 */

export const dynamic = "force-dynamic";

const SPECIMEN = [
  { line: 1, description: "Hex bolt M8x40 zinc plated, DIN 933, boxed 100" },
  { line: 2, description: "Hex bolt M8x41 zinc plated, DIN 933, boxed 100" },
  { line: 3, description: "Hex bolt M8x42 zinc plated, DIN 933, boxed 100" },
];

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
  { user: "controller", sees: "Oversight: reads every desk's queue, owns no exception" },
  { user: "auditor", sees: "Everything, and can decide nothing" },
];

export default async function Home() {
  const session = await readSession();
  if (session) redirect("/queue");

  return (
    <>
      <Masthead />
      <main id="main">
        <div className="door">
          <div>
            <h1>Invoices, checked before they are paid</h1>
            <p className="lede">
              Threshold decides which supplier invoices can be paid automatically, and puts the rest
              in front of the one person who can resolve them. Every screen here is backed by a real
              decision recorded in a real database — nothing on it is a mock.
            </p>
            <p className="actions">
              <a className="button primary" href="/api/auth/login">
                Sign in to the console
              </a>
            </p>
            <p className="note">
              Start as <code>buyer</code>, password the same. It owns price disputes, which is what
              most of the blocked invoices in this demo are.
            </p>
          </div>

          <figure className="specimen" aria-labelledby="specimen-label">
            <p className="run print">
              <span>
                <b>Northgate Supply</b>
              </span>
              <span className="figure">
                899.64<span className="unit">EUR</span>
              </span>
              <span className="red">Blocked</span>
            </p>
            <table className="lines stack">
              <thead>
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
                    Agreed
                  </th>
                  <th scope="col" className="num">
                    Billed
                  </th>
                </tr>
              </thead>
              <tbody>
                {SPECIMEN.map((row) => (
                  <tr key={row.line}>
                    <td data-label="Line" className="print">
                      {row.line}
                    </td>
                    <td data-label="Description" className="desc">
                      {row.description}
                    </td>
                    <td data-label="Ordered" className="num">
                      12
                    </td>
                    <td data-label="Received" className="num">
                      12
                    </td>
                    <td data-label="Agreed price" className="num">
                      18.40
                    </td>
                    <td data-label="Billed price" className="num over">
                      21.00
                      <span className="mark">2.60 over</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="routed print">
              <span>Billed above the agreed price · lines 1, 2, 3</span>
              <span>Buyer · due within 48 h</span>
            </p>
            <figcaption className="label" id="specimen-label">
              A synthetic invoice from this project&apos;s demo data, printed the way the console
              prints it. Quantities agree; the price does not, so it waits for the buyer.
            </figcaption>
          </figure>
        </div>

        <h2>The problem it solves</h2>
        <div className="prose">
          <p>
            Before a company pays an invoice, somebody has to check it against what was ordered and
            what actually arrived. Done by hand that is slow, and the things it misses are
            expensive: the same invoice paid twice, a unit price that crept up between the order and
            the bill, or a supplier&apos;s bank details changed by somebody who is not the supplier.
          </p>
          <p className="muted">
            That last one is the reason this exists. It is the fraud this is built to catch, and it
            arrives looking like an ordinary invoice from a vendor you really do owe money to.
          </p>
        </div>

        <h2>Follow one invoice through</h2>
        <ol className="steps">
          {STEPS.map((step) => (
            <li key={step.title}>
              <strong>{step.title}</strong>
              <p>{step.detail}</p>
            </li>
          ))}
        </ol>

        <h2>What is unusual about it</h2>
        <table className="properties stack">
          <thead>
            <tr>
              <th scope="col">Property</th>
              <th scope="col">How it holds</th>
            </tr>
          </thead>
          <tbody>
            {PROPERTIES.map((property) => (
              <tr key={property.title}>
                <th scope="row" data-label="Property">
                  {property.title}
                </th>
                <td data-label="How it holds">{property.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <h2>Have a look</h2>
        <p className="lede">
          Six demo accounts, one per role, each with the password the same as the username. Which
          invoices you can act on depends on which exceptions you own — that is the authorisation
          rule rather than a display filter, and you can watch it refuse you.
        </p>
        <table className="stack">
          <thead>
            <tr>
              <th scope="col">Sign in as</th>
              <th scope="col">Handles</th>
            </tr>
          </thead>
          <tbody>
            {ACCOUNTS.map((account) => (
              <tr key={account.user}>
                <td data-label="Sign in as">
                  <code>{account.user}</code>
                </td>
                <td data-label="Handles">{account.sees}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="close">
          <a className="button primary" href="/api/auth/login">
            Sign in to the console
          </a>
          <p>The queue is the product, and the invoice printed above is waiting in it.</p>
        </div>
      </main>
    </>
  );
}
