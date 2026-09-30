import Link from "next/link";

import { roleName } from "@/lib/format";
import { readSession } from "@/lib/session";

/**
 * The top of every page: the name, the two places there are to go, and who is
 * reading.
 *
 * Rendered by each page rather than the layout, because only the page knows
 * which of the two it is, and `aria-current` is how a screen reader learns it.
 */
export async function Masthead({ current }: { current?: "queue" | "ledger" }) {
  const session = await readSession();
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <Link className="brand" href="/">
          Threshold
        </Link>
        {session && (
          <nav aria-label="Console">
            <a href="/queue" aria-current={current === "queue" ? "page" : undefined}>
              Queue
            </a>
            <a href="/ledger" aria-current={current === "ledger" ? "page" : undefined}>
              Books
            </a>
          </nav>
        )}
        <div className="reader">
          {session ? (
            <>
              <span>
                Reading as <b className="print">{session.username}</b>
                {" · "}
                {session.roles.map(roleName).join(", ") || "no roles"}
              </span>
              <form action="/api/auth/logout" method="post">
                <button type="submit" className="quiet">
                  Sign out
                </button>
              </form>
            </>
          ) : (
            <a href="/api/auth/login">Sign in</a>
          )}
        </div>
      </div>
    </header>
  );
}
