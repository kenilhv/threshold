import { NextResponse } from "next/server";

/**
 * Liveness for the container, and nothing more.
 *
 * The healthcheck used to fetch /queue. That redirects to login, which
 * redirects to Keycloak's *public* URL, and fetch follows redirects -- so the
 * check was really asking whether localhost:8080 is reachable from inside this
 * container, which it never is. The console reported unhealthy while serving
 * pages correctly. A liveness probe answers "is this process able to respond",
 * and a probe that depends on a third service's public hostname answers a
 * different question on the wrong network.
 */
export const dynamic = "force-dynamic";

export function GET(): NextResponse {
  return NextResponse.json({ status: "ok" });
}
