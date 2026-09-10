import type { NextConfig } from "next";

const config: NextConfig = {
  // The console is deployed as a container in the same compose network, so the
  // standalone output is what keeps the runtime image small enough to be worth
  // building at all: it traces the modules actually imported instead of copying
  // node_modules.
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
};

export default config;
