import { redirect } from "next/navigation";

import { Masthead } from "@/components/masthead";
import { ApiError, getTrialBalance, type TrialBalance } from "@/lib/api";
import { magnitude } from "@/lib/format";
import { readSession } from "@/lib/session";

/**
 * Where the money is, and whether the books add up.
 *
 * The ledger was a whole milestone and had no screen. Its guarantees lived in
 * a migration, a trigger and a test file, which is the right place for them to
 * be *enforced* and the wrong place for them to be *seen* — somebody evaluating
 * this project would have had to read the schema to learn it existed.
 *
 * Printed as a trial balance is printed: a debit column and a credit column,
 * so a liability reads as a credit instead of as a minus sign somebody has to
 * be told is healthy. Which column a balance goes in is its sign, read off the
 * API's own number; nothing is added up here.
 *
 * The number that matters is the last one. A trial balance summing to anything
 * but zero means something reached `ledger_entry` without going through either
 * the domain rules or the database's own balance check, which is a short list:
 * a bad restore, a migration that moved rows, or manual SQL during an incident.
 * It is not a number that drifts. It is zero or there is an incident.
 */

export const dynamic = "force-dynamic";

/** What each account is for, in the language of the business rather than the schema. */
const MEANING: Record<string, string> = {
  accounts_payable: "What we owe suppliers. Credited when an invoice is approved, debited when it is paid.",
  cash: "The bank account the money leaves.",
  cash_in_transit:
    "Sent to the payment rail, not yet confirmed. This account exists because 'sent' and 'arrived' are different facts.",
  expense: "Where an approved invoice lands on the other side of the accrual.",
  opening_balance: "The starting position, so the first payment is not an overdraft.",
};

/** Which side of a balance is the healthy one, for each kind of account. */
const NORMAL: Record<string, "debit" | "credit"> = {
  asset: "debit",
  expense: "debit",
  liability: "credit",
  equity: "credit",
};

function reading(kind: string, amountMinor: number): string {
  if (amountMinor === 0) return "nothing here yet";
  const side = amountMinor > 0 ? "debit" : "credit";
  return side === NORMAL[kind] ? "normal for this account" : "the opposite of its normal side";
}

export default async function LedgerPage() {
  const session = await readSession();
  if (!session) redirect("/api/auth/login?return=/ledger");

  let balance: TrialBalance;
  try {
    balance = await getTrialBalance();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      redirect("/api/auth/login?return=/ledger");
    }
    throw error;
  }

  return (
    <>
      <Masthead current="ledger" />
      <main id="main">
        <div className="report-head">
          <h1>The books</h1>
          <p className="run print">
            <span>Trial balance</span>
            <span>
              in <b>{balance.currency}</b>, as stored
            </span>
          </p>
        </div>
        <p className="lede">
          Every movement of money in this system is a set of entries that sum to zero. Postgres
          enforces that at commit time rather than any application code doing it, and it enforces it
          against edits and deletions too — so an entry already written cannot be quietly removed to
          make something else balance.
        </p>

        <table className="ledger stack">
          <caption className="sr">Account balances in {balance.currency}, debit and credit</caption>
          <thead>
            <tr>
              <th scope="col">Account</th>
              <th scope="col" className="num">
                Debit
              </th>
              <th scope="col" className="num">
                Credit
              </th>
              <th scope="col">Reading</th>
            </tr>
          </thead>
          <tbody>
            {balance.accounts.map((account) => (
              <tr key={account.account}>
                <td data-label="Account" className="account">
                  {account.account.replace(/_/g, " ")}
                  <span className="meaning">{MEANING[account.account] ?? account.kind}</span>
                </td>
                <td data-label="Debit" className="num">
                  {account.amount_minor > 0 ? magnitude(account.amount) : ""}
                </td>
                <td data-label="Credit" className="num">
                  {account.amount_minor < 0 ? magnitude(account.amount) : ""}
                </td>
                <td data-label="Reading" className="muted">
                  {account.kind} · {reading(account.kind, account.amount_minor)}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td data-label="Difference">Debits less credits</td>
              <td data-label="Amount" className="num" colSpan={2}>
                {balance.total_minor === 0 ? (
                  "0.00"
                ) : (
                  <span className="red">{balance.total_minor} minor units</span>
                )}
              </td>
              <td data-label="Result" className={`status${balance.balanced ? "" : " red"}`}>
                {balance.balanced ? "Balanced" : "Not balanced"}
              </td>
            </tr>
          </tfoot>
        </table>

        <div className="balance">
          <span className={`status${balance.balanced ? "" : " red"}`}>
            {balance.balanced ? "Balanced" : "Not balanced"}
          </span>
          <p>
            {balance.balanced
              ? "Every account summed together comes to exactly zero, which is the whole of the check and the only number on this page that has to be right."
              : `The accounts sum to ${balance.total_minor} minor units rather than zero. Something reached the ledger without going through either the domain rules or the database's balance check. Nothing else on this page can be trusted until that is explained.`}
          </p>
        </div>
      </main>
    </>
  );
}
