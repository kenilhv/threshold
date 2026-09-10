import { randomBytes } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

import { authorizeUrl, newVerifier } from "@/lib/oidc";
import { stashVerifier } from "@/lib/session";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const returnTo = request.nextUrl.searchParams.get("return") ?? "/queue";
  // Only relative paths. An open redirect on a login route is how a phishing
  // page borrows a real login screen.
  const safeReturn = returnTo.startsWith("/") && !returnTo.startsWith("//") ? returnTo : "/queue";

  const verifier = newVerifier();
  const state = randomBytes(16).toString("base64url");
  await stashVerifier(`${verifier}:${state}`, safeReturn);
  return NextResponse.redirect(authorizeUrl(verifier, state));
}
