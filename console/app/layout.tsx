import type { Metadata } from "next";
import { Archivo, Martian_Mono } from "next/font/google";

import "./globals.css";

/*
 * Two faces, because the page has two voices. Archivo is the form: headings,
 * column titles and instructions, set condensed the way preprinted business
 * stationery was. Martian Mono is the printer: every figure, date and code the
 * system wrote, with tabular numerals so a column of amounts lines up by
 * digit. Both self-hosted at build time; no request leaves for a font CDN.
 */
const form = Archivo({
  subsets: ["latin"],
  axes: ["wdth"],
  style: ["normal", "italic"],
  variable: "--font-form",
  display: "swap",
});

const print = Martian_Mono({
  subsets: ["latin"],
  axes: ["wdth"],
  variable: "--font-print",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Threshold — operator console",
  description: "Invoices the system would not clear on its own.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${form.variable} ${print.variable}`}>
      <body>
        {/* Keyboard users reach the work without tabbing the masthead first. */}
        <a className="skip" href="#main">
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
