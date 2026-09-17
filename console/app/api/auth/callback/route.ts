import { NextRequest, NextResponse } from "next/server";

import { CONSOLE_ORIGIN, exchange } from "@/lib/oidc";
import { takeVerifier, writeSession } from "@/lib/session";

type Claims = { preferred_username?: string; realm_access?: { roles?: string[] } };

/** Read the payload for display only. The API verifies the signature. */
function claimsOf(token: string): Claims {
  const payload = token.split(".")[1];
  if (!payload) return {};
  try {
    return JSON.parse(Buffer.from(payload, "base64url").toString()) as Claims;
  } catch {
    return {};
  }
}

// Every redirect out of this route is built on CONSOLE_ORIGIN, never on the
// request. In a standalone Next build inside a container the request's URL
// carries the address the server *binds* -- 0.0.0.0:3000 -- not the address the
// browser used, so a redirect based on it sends a freshly signed-in user to
// http://0.0.0.0:3000/queue and an error page. The inbound half of this flow
// (REDIRECT_URI) was always built from the configured origin; the outbound
// half has to be built the same way, for the same reason.
export async function GET(request: NextRequest): Promise<NextResponse> {
  const code = request.nextUrl.searchParams.get("code");
  const state = request.nextUrl.searchParams.get("state");
  const { verifier: stashed, returnTo } = await takeVerifier();

  if (!code || !state || !stashed) {
    return NextResponse.redirect(new URL("/?error=incomplete", CONSOLE_ORIGIN));
  }

  const [verifier, expectedState] = stashed.split(":");
  // Without this the callback is forgeable: an attacker can hand a victim's
  // browser their own authorization code and have the session belong to them.
  if (!verifier || state !== expectedState) {
    return NextResponse.redirect(new URL("/?error=state", CONSOLE_ORIGIN));
  }

  try {
    const token = await exchange(code, verifier);
    const claims = claimsOf(token.access_token);
    await writeSession({
      accessToken: token.access_token,
      username: claims.preferred_username ?? "unknown",
      roles: claims.realm_access?.roles ?? [],
      expiresAt: Math.floor(Date.now() / 1000) + token.expires_in,
    });
  } catch {
    return NextResponse.redirect(new URL("/?error=exchange", CONSOLE_ORIGIN));
  }

  return NextResponse.redirect(new URL(returnTo, CONSOLE_ORIGIN));
}
