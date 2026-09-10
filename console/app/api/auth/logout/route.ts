import { NextResponse } from "next/server";

import { logoutUrl } from "@/lib/oidc";
import { clearSession } from "@/lib/session";

export async function POST(): Promise<NextResponse> {
  // POST, not GET: a logout on GET can be triggered by any image tag on any
  // page, which is a nuisance rather than a breach and is still avoidable.
  await clearSession();
  return NextResponse.redirect(logoutUrl(), { status: 303 });
}
