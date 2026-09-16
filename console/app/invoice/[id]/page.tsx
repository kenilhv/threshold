import { redirect } from "next/navigation";

import { DecideForm } from "@/components/decide-form";
import {
  ApiError,
  getDecisions,
  getMatch,
  type DecisionView,
  type MatchView,
} from "@/lib/api";
import { readSession } from "@/lib/session";

/**
 * One invoice, its evidence, and the decision if this person may make it.
 *
 * `skipped_checks` is rendered at the same weight as the exceptions, and that
 * is the whole argument of this screen. A clean result only means something if
 * every check ran; an absent check and a passing check look identical from the
 * outside, and the difference is the entire value of the answer.
 *
 * The agent's escalation is rendered last and visibly apart, attributed to the
 * tier that wrote it and quoted rather than stated. Everything above it is
 * something this system established; that block is something a model said after
 * reading a document the supplier wrote. A screen that presented both in the
 * same voice would be the last place an injected instruction has to get to.
 */

export const dynamic = "force-dynamic";

function ownersOf(match: MatchView): string[] {
  return [...new Set(match.exceptions.map((e) => e.owner_role))].sort();
}

function canDecide(match: MatchView, roles: string[]): boolean {
  const owners = ownersOf(match);
  if (owners.length === 0) return false;
  if (roles.includes("auditor")) return false;
  return owners.every((owner) => roles.includes(owner));
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
  const settled = history.some((d) => d.decision === "approved" || d.decision === "rejected");

  return (
    <>
      <p className="meta">
        <a href="/queue">← Queue</a>
      </p>

      <h2>{match.vendor ? match.vendor.display_name : "Vendor did not resolve"}</h2>
      <p className="lede">
        Judged under policy <code>{match.policy_version}</code> on evidence version{" "}
        {match.version}.{" "}
        {match.vendor
          ? `${match.vendor.vendor_number} · ${match.vendor.country_code} · ${match.vendor.status}`
          : "Nothing on this invoice matched a vendor on file."}
      </p>

      <div className="card">
        <h3>
          <span
            className={`tag ${
              match.disposition === "blocked"
                ? "block"
                : match.disposition === "needs_review"
                  ? "review"
                  : "clear"
            }`}
          >
            {match.disposition.replace(/_/g, " ")}
          </span>
        </h3>

        {match.exceptions.length === 0 && (
          <p className="meta">No exceptions were raised.</p>
        )}

        {match.exceptions.map((exception) => (
          <div
            key={`${exception.code}-${exception.invoice_line_number}-${exception.purchase_order_line_number}`}
            className={`exception ${exception.severity === "block" ? "block" : "review"}`}
          >
            <strong>{exception.code.replace(/_/g, " ")}</strong>{" "}
            <span className={`tag ${exception.severity === "block" ? "block" : "review"}`}>
              {exception.severity}
            </span>{" "}
            <span className="meta" style={{ display: "inline" }}>
              → {exception.owner_role}
            </span>
            <p>{exception.detail}</p>
            {exception.breach ? (
              <p className="meta">
                expected {String(exception.breach.expected)} · billed{" "}
                {String(exception.breach.actual)} · over by {String(exception.breach.variance)}
              </p>
            ) : null}
          </div>
        ))}

        {/* Same weight as the exceptions, deliberately. A check that did not
            run is not the same as a check that passed. */}
        {match.skipped_checks.map((skipped) => (
          <div key={skipped} className="skipped">
            <strong>Not checked</strong>
            <p>{skipped}</p>
          </div>
        ))}

        <p className="meta">
          {match.line_pairs.length} invoice line{match.line_pairs.length === 1 ? "" : "s"} paired
          with the order
        </p>
      </div>

      {/* The money, if it has started moving. Above the agent's opinion
          because it is a fact about this invoice rather than a suggestion
          about it, and a reviewer scanning downward should meet the facts
          first. */}
      {match.payment && (
        <div className="card">
          <h3>Payment</h3>
          <p>
            <strong>
              {match.payment.amount} {match.payment.currency}
            </strong>
            {" — "}
            {match.payment.state === "submitting"
              ? "instruction accounted for, awaiting the rail"
              : match.payment.state}
          </p>
          {match.payment.failure_reason && (
            <p className="meta">Refused: {match.payment.failure_reason}</p>
          )}
          {match.payment.rail_attempts > 1 && (
            <p className="meta">
              The rail was asked {match.payment.rail_attempts} times. Each retry sent the
              same idempotency key, so the money moved at most once.
            </p>
          )}
          <p className="meta">
            {match.payment.rail_id}
            {match.payment.rail_reference ? ` · ${match.payment.rail_reference}` : ""}
          </p>
        </div>
      )}

      {/* Rendered after the evidence and visibly apart from it. The exceptions
          above are things the system established; this is something an agent
          said, and a reviewer must never have to guess which they are reading.

          The attribution line is not decoration. The summary was written by a
          model that read this supplier's own line descriptions, so on a hostile
          invoice it is the supplier talking at one remove. Naming the author is
          what keeps that legible. */}
      {match.escalation && (
        <div className="card agent">
          <h3>Suggested by {match.escalation.triager_id}</h3>
          <p className="meta">
            An opinion about what to look at first. It changes nothing above: every
            exception keeps the owner and the deadline it was raised with.
          </p>
          <p>
            <strong>{match.escalation.owner_role.replace(/_/g, " ")} first</strong>
            {" — "}
            {match.escalation.codes.join(", ").replace(/_/g, " ")}
          </p>
          <blockquote>{match.escalation.summary}</blockquote>
          {match.escalation.uncited_codes.length > 0 && (
            <p className="meta">
              Did not mention: {match.escalation.uncited_codes.join(", ").replace(/_/g, " ")}.
              Those exceptions are still open and still owned.
            </p>
          )}
        </div>
      )}

      {history.length > 0 && (
        <div className="card">
          <h3>Decisions</h3>
          <p className="meta">Append-only. This is the whole history, not the current state.</p>
          {history.map((decision) => (
            <div key={decision.id} className="exception">
              <strong>{decision.decision.replace(/_/g, " ")}</strong> ·{" "}
              {decision.actor_label ?? "system"} ·{" "}
              <span className="meta" style={{ display: "inline" }}>
                {new Date(decision.decided_at).toLocaleString()}
              </span>
              {decision.reason ? <p>{decision.reason}</p> : null}
              {decision.overrode.length > 0 ? (
                <p className="meta">cleared: {decision.overrode.join(", ")}</p>
              ) : null}
            </div>
          ))}
        </div>
      )}

      {settled ? (
        <p className="meta">This invoice has been decided. There is no undo.</p>
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
          <strong>You cannot decide this invoice.</strong>
          <p>
            {owners.length === 0
              ? "It raised no exceptions, so there is nothing to decide."
              : session.roles.includes("auditor")
                ? "An auditor may read everything and decide nothing."
                : `It raised exceptions owned by ${owners.join(" and ")}. You hold ${
                    session.roles.join(", ") || "no roles"
                  }.`}
          </p>
        </div>
      )}
    </>
  );
}
