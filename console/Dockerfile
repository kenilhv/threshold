# Same shape as the Python image: a build stage with the toolchain, a runtime
# stage without it, running as a non-root user. A container that can compile and
# runs as root is two unnecessary rungs on an attacker's ladder, and that
# reasoning does not stop applying because the language changed.

# ----------------------------------------------------------------- deps ---
FROM node:24-slim AS deps
WORKDIR /build

# Copied alone so this layer is cached until the dependency set actually
# changes, rather than on every source edit.
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

# ---------------------------------------------------------------- build ---
FROM node:24-slim AS build
WORKDIR /build

ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /build/node_modules ./node_modules
COPY . .
RUN npm run build

# -------------------------------------------------------------- runtime ---
FROM node:24-slim AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN groupadd --system --gid 1001 console \
 && useradd --system --uid 1001 --gid console --create-home console

# `output: standalone` traces the modules actually imported, so this copies a
# server and its real dependencies rather than the whole node_modules tree.
COPY --from=build --chown=console:console /build/.next/standalone ./
COPY --from=build --chown=console:console /build/.next/static ./.next/static

USER console

EXPOSE 3000

CMD ["node", "server.js"]
