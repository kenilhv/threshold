// `next lint` was removed in Next 16 and now reads "lint" as a directory name,
// so the script that called it had been failing since it was written. This is
// the flat-config replacement: Next's core-web-vitals rules plus its TypeScript
// rules, run by ESLint directly.
import coreWebVitals from "eslint-config-next/core-web-vitals";
import typescript from "eslint-config-next/typescript";

const config = [
  ...coreWebVitals,
  ...typescript,
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
];

export default config;
