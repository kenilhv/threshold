import { cookies } from "next/headers";

/**
 * The console is a back-end for front-end: no page script can read the access
 * token.
 *
 * Stated precisely, because an earlier version of this comment said "the
 * browser never holds an access token" and that is false. The browser holds it:
 * `writeSession` base64url-encodes this whole session object -- token included
 * -- into a cookie. Encoding, not encryption, and anyone who opens this file
 * can see it. What the cookie's `httpOnly` flag buys is that `document.cookie`
 * and every other page script cannot read it, which is the property the XSS
 * argument below actually needs.
 *
 * The residual, since the difference is the point: a stolen *cookie* is a
 * bearer token usable directly against the API until it expires, not merely a
 * console session. `sameSite: "lax"` and `secure` in production are what narrow
 * the ways it gets stolen.
 *
 * The alternative — a public client doing PKCE in the browser and keeping the
 * token in memory or storage — is a legitimate pattern, and it is the wrong one
 * here. This console authorises payment. A single cross-site scripting bug in a
 * page that renders vendor-supplied invoice text would hand an attacker a
 * bearer token for an API that approves money. Keeping the token server-side
 * means the worst an injected script can do is act as the user *through* this
 * origin, which the same-site cookie and the audit trail both constrain.
 *
 * The session cookie is httpOnly, sameSite=lax and — outside local development
 * — secure. `lax` rather than `strict` because the OIDC callback is a
 * cross-site navigation back from Keycloak and a strict cookie would not be
 * sent with it, which is the exact request that needs it.
 */

const SESSION_COOKIE = "threshold_session";
const VERIFIER_COOKIE = "threshold_pkce";
const RETURN_COOKIE = "threshold_return";

export type Session = {
  accessToken: string;
  username: string;
  roles: string[];
  /** Unix seconds. Compared before use so an expired token is a fresh login. */
  expiresAt: number;
};

function secure(): boolean {
  return process.env.NODE_ENV === "production" && process.env.THRESHOLD_INSECURE_COOKIES !== "1";
}

export async function readSession(): Promise<Session | null> {
  const raw = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!raw) return null;
  try {
    const session = JSON.parse(Buffer.from(raw, "base64url").toString()) as Session;
    // An expired token would produce a 401 from the API and a confusing empty
    // screen. Treating it as no session sends the user to log in, which is what
    // actually needs to happen.
    if (session.expiresAt * 1000 <= Date.now()) return null;
    return session;
  } catch {
    return null;
  }
}

export async function writeSession(session: Session): Promise<void> {
  (await cookies()).set(SESSION_COOKIE, Buffer.from(JSON.stringify(session)).toString("base64url"), {
    httpOnly: true,
    sameSite: "lax",
    secure: secure(),
    path: "/",
    maxAge: Math.max(0, session.expiresAt - Math.floor(Date.now() / 1000)),
  });
}

export async function clearSession(): Promise<void> {
  const jar = await cookies();
  for (const name of [SESSION_COOKIE, VERIFIER_COOKIE, RETURN_COOKIE]) {
    jar.delete(name);
  }
}

export async function stashVerifier(verifier: string, returnTo: string): Promise<void> {
  const jar = await cookies();
  const options = {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: secure(),
    path: "/",
    maxAge: 600,
  };
  jar.set(VERIFIER_COOKIE, verifier, options);
  jar.set(RETURN_COOKIE, returnTo, options);
}

export async function takeVerifier(): Promise<{ verifier: string | null; returnTo: string }> {
  const jar = await cookies();
  return {
    verifier: jar.get(VERIFIER_COOKIE)?.value ?? null,
    returnTo: jar.get(RETURN_COOKIE)?.value ?? "/queue",
  };
}
