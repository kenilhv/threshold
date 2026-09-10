import { createHash, randomBytes } from "node:crypto";

/**
 * Authorization code with PKCE, by hand.
 *
 * A library would be defensible and would also hide the four things that
 * actually matter here, which are the four things a reviewer of this project
 * should be able to see: the `state` parameter that makes the callback
 * unforgeable, the code verifier that makes an intercepted authorization code
 * useless, the fact that the token exchange happens server-side, and the split
 * between the issuer the browser is redirected to and the one this container
 * can reach.
 *
 * That last one is not incidental complexity. Keycloak issues tokens claiming
 * `http://localhost:8080/...` because that is where the browser talks to it,
 * while this container reaches it at `http://keycloak:8080/...`. Deriving one
 * from the other is what makes a correct issuer check reject every real token.
 */

/** Where the browser is sent. Must match the `iss` the API validates. */
const PUBLIC_ISSUER =
  process.env.THRESHOLD_OIDC_PUBLIC_ISSUER ?? "http://localhost:8080/realms/threshold";

/** Where this container performs the token exchange. */
const INTERNAL_ISSUER =
  process.env.THRESHOLD_OIDC_INTERNAL_ISSUER ?? "http://keycloak:8080/realms/threshold";

const CLIENT_ID = process.env.THRESHOLD_OIDC_CLIENT_ID ?? "threshold-console";

export const CONSOLE_ORIGIN = process.env.THRESHOLD_CONSOLE_ORIGIN ?? "http://localhost:3000";
export const REDIRECT_URI = `${CONSOLE_ORIGIN}/api/auth/callback`;

export function newVerifier(): string {
  return randomBytes(32).toString("base64url");
}

export function challengeFor(verifier: string): string {
  return createHash("sha256").update(verifier).digest("base64url");
}

export function authorizeUrl(verifier: string, state: string): string {
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    response_type: "code",
    scope: "openid profile email",
    redirect_uri: REDIRECT_URI,
    code_challenge: challengeFor(verifier),
    code_challenge_method: "S256",
    state,
  });
  return `${PUBLIC_ISSUER}/protocol/openid-connect/auth?${params}`;
}

export type TokenResponse = {
  access_token: string;
  expires_in: number;
};

export async function exchange(code: string, verifier: string): Promise<TokenResponse> {
  const response = await fetch(`${INTERNAL_ISSUER}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: CLIENT_ID,
      code,
      redirect_uri: REDIRECT_URI,
      code_verifier: verifier,
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`token exchange failed: ${response.status} ${await response.text()}`);
  }
  return (await response.json()) as TokenResponse;
}

export function logoutUrl(): string {
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    post_logout_redirect_uri: CONSOLE_ORIGIN,
  });
  return `${PUBLIC_ISSUER}/protocol/openid-connect/logout?${params}`;
}
