# Multi-stage. The runtime image carries no build toolchain and runs as a
# non-root user, because a container that can compile and runs as root is two
# unnecessary rungs on an attacker's ladder.

# ---------------------------------------------------------------- builder ---
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Dependencies resolve from pyproject alone, so this layer is cached until the
# dependency set actually changes rather than on every source edit.
COPY pyproject.toml README.md ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && mkdir -p src/threshold && touch src/threshold/__init__.py \
 && /opt/venv/bin/pip install .

# ---------------------------------------------------------------- runtime ---
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # /app as well as /app/src: the operational CLI lives in scripts/ and is
    # run inside this image (python -m scripts.queuectl), so it has to be
    # importable, not just the package.
    PYTHONPATH=/app/src:/app

RUN groupadd --system --gid 1001 threshold \
 && useradd --system --uid 1001 --gid threshold --create-home threshold

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=threshold:threshold src/ ./src/
COPY --chown=threshold:threshold migrations/ ./migrations/
COPY --chown=threshold:threshold scripts/ ./scripts/
COPY --chown=threshold:threshold seeds/ ./seeds/
COPY --chown=threshold:threshold alembic.ini pyproject.toml ./

USER threshold

EXPOSE 8000

CMD ["uvicorn", "threshold.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
