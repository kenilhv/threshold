import type { Metadata } from "next";

import { readSession } from "@/lib/session";
import "./globals.css";

export const metadata: Metadata = {
  title: "Threshold — operator console",
  description: "Invoices the system would not clear on its own.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await readSession();
  return (
    <html lang="en">
      <body>
        {/* Keyboard users reach the queue without tabbing the header first. */}
        <a className="skip" href="#main">
          Skip to content
        </a>
        <header className="bar">
          <h1>Threshold</h1>
          <a href="/queue">Queue</a>
          <a href="/ledger">Books</a>
          {session ? (
            <span className="who">
              {session.username} · {session.roles.join(", ") || "no roles"}
              <form action="/api/auth/logout" method="post" style={{ display: "inline" }}>
                <button type="submit" style={{ marginLeft: "0.75rem", padding: "0.2rem 0.6rem" }}>
                  Sign out
                </button>
              </form>
            </span>
          ) : (
            <span className="who">
              <a href="/api/auth/login">Sign in</a>
            </span>
          )}
        </header>
        <main id="main">{children}</main>
      </body>
    </html>
  );
}
