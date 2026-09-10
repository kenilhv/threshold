"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiError, postDecision } from "@/lib/api";

/**
 * The write path.
 *
 * The refusals are rendered, not retried and not thrown away into a toast. A
 * 409 here means one of two specific things — the evidence moved, or a
 * colleague decided first — and both are information the reviewer needs in
 * order to do the right next thing, which is to look at the invoice again. A
 * toast saying "please try again" would teach them that re-deciding is noise.
 */

export type DecisionState =
  | { status: "idle" }
  | { status: "conflict"; message: string; currentVersion?: number }
  | { status: "refused"; message: string };

function messageOf(body: unknown, fallback: string): { message: string; version?: number } {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return { message: detail };
    if (detail && typeof detail === "object") {
      const record = detail as Record<string, unknown>;
      const text = typeof record.detail === "string" ? record.detail : fallback;
      const roles = Array.isArray(record.required_roles) ? record.required_roles : null;
      return {
        message: roles && roles.length > 0 ? `${text} (needs: ${roles.join(", ")})` : text,
        version:
          typeof record.current_version === "number" ? record.current_version : undefined,
      };
    }
  }
  return { message: fallback };
}

export async function submitDecision(
  _previous: DecisionState,
  form: FormData,
): Promise<DecisionState> {
  const matchResultId = String(form.get("matchResultId") ?? "");
  const documentId = String(form.get("documentId") ?? "");
  const decision = String(form.get("decision") ?? "");
  const reason = String(form.get("reason") ?? "").trim();
  const seenVersion = Number(form.get("seenVersion") ?? 0);

  if (reason.length < 3) {
    return { status: "refused", message: "A decision must say why." };
  }

  try {
    await postDecision(matchResultId, { decision, reason, seen_version: seenVersion });
  } catch (error) {
    if (error instanceof ApiError) {
      const { message, version } = messageOf(error.body, "The decision was not recorded.");
      if (error.status === 409) {
        return { status: "conflict", message, currentVersion: version };
      }
      if (error.status === 403 || error.status === 422) {
        return { status: "refused", message };
      }
      if (error.status === 401) redirect(`/api/auth/login?return=/invoice/${documentId}`);
    }
    throw error;
  }

  revalidatePath("/queue");
  revalidatePath(`/invoice/${documentId}`);
  redirect(`/invoice/${documentId}`);
}
