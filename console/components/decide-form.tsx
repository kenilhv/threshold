"use client";

import { useActionState } from "react";

import { submitDecision, type DecisionState } from "@/app/invoice/[id]/actions";

/**
 * The only client component in this application.
 *
 * It is a client component for exactly one reason: the result of a submission
 * has to be announced to a screen reader, and that needs a live region that
 * survives the response. Everything else here is a plain form posting to a
 * server action — no controlled inputs, no form library, no client validation
 * duplicating the server's.
 *
 * There is deliberately no `useOptimistic`. Optimistic UI renders a state the
 * server has not agreed to, and the entire design premise of this endpoint is
 * that the server may refuse: the evidence may have moved, or a colleague may
 * have decided first. Showing "approved" and taking it back is the one
 * interaction pattern this screen must not have.
 */

const initial: DecisionState = { status: "idle" };

export function DecideForm({
  matchResultId,
  documentId,
  seenVersion,
  clearing,
}: {
  matchResultId: string;
  documentId: string;
  seenVersion: number;
  clearing: string[];
}) {
  const [state, action, pending] = useActionState(submitDecision, initial);

  return (
    <form className="decide" action={action}>
      <input type="hidden" name="matchResultId" value={matchResultId} />
      <input type="hidden" name="documentId" value={documentId} />
      {/* The version the reviewer actually looked at. If the matcher writes a
          new one while this page is open, the server refuses rather than
          recording a judgement about evidence nobody read. */}
      <input type="hidden" name="seenVersion" value={seenVersion} />

      <label htmlFor="decision">Decision</label>
      <select id="decision" name="decision" defaultValue="approved">
        <option value="approved">Approve for payment</option>
        <option value="rejected">Reject</option>
        <option value="held">Hold</option>
      </select>

      <p className="hint" style={{ marginTop: "0.75rem" }}>
        {clearing.length > 0
          ? `This will be recorded as clearing: ${clearing.join(", ")}.`
          : "Nothing will be recorded as cleared."}
      </p>

      <label htmlFor="reason" style={{ marginTop: "0.75rem" }}>
        Why
      </label>
      <p className="hint">
        Required. It is the only part of this record a later reader cannot work out from the
        others.
      </p>
      <textarea
        id="reason"
        name="reason"
        required
        minLength={3}
        maxLength={2000}
        aria-describedby="reason-hint"
        placeholder="Called the vendor on the number we hold on file, not the one printed here."
      />

      <div className="actions">
        <button type="submit" className="primary" disabled={pending}>
          {pending ? "Recording…" : "Record decision"}
        </button>
        <a href="/queue">Back to the queue</a>
      </div>

      {/* Announced, not just rendered. A reviewer who cannot see the page still
          has to learn that their approval was refused. */}
      <div aria-live="assertive" role="status">
        {state.status === "conflict" && (
          <div className="conflict">
            <strong>Not recorded.</strong>
            <p>{state.message}</p>
            {state.currentVersion !== undefined && (
              <p>
                You were reading version {seenVersion}; version {state.currentVersion} is current.{" "}
                <a href={`/invoice/${documentId}`}>Reload the invoice</a> and look again.
              </p>
            )}
          </div>
        )}
        {state.status === "refused" && (
          <div className="refusal">
            <strong>Not recorded.</strong>
            <p>{state.message}</p>
          </div>
        )}
      </div>
    </form>
  );
}
