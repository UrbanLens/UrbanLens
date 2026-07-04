# Allow future upgrades by pinning the base image version here
ARG PYTHON_BASE_IMAGE_VERSION=3.12-bookworm

FROM python:${PYTHON_BASE_IMAGE_VERSION} AS base

# Controls which dependency groups uv installs below. staging/production
# install only [project.dependencies] (--no-dev, no linters/test tools/debug
# toolbar); everything else (local, development, testing) also installs the
# `dev` dependency-group from pyproject.toml.
ARG UL_ENVIRONMENT=production

# Ensure logging dir exists at /var/log/urbanlens
RUN mkdir -p /var/log/urbanlens

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# Install system dependencies and PostgreSQL 17 client from PGDG.
# The versioned binary at /usr/lib/postgresql/17/bin/pg_dump is used directly
# (via UL_PG_DUMP_BIN) to avoid the pg_wrapper dispatcher requiring a running
# local cluster.
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive && \
    apt-get install -y --no-install-recommends ca-certificates curl gcc pkg-config gnupg && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc && \
    . /etc/os-release && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        locales \
        unzip \
        postgresql-client-17 \
        git \
        iputils-ping \
        libgdal-dev \
        wget \
        gosu && \
    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && \
    locale-gen && \
    pg_dump --version && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LC_CTYPE=en_US.UTF-8

# Install Bun (JS runtime + package manager) via the official Docker image.
# This avoids the curl-to-bash NVM install and gives a reproducible binary.
COPY --from=oven/bun:1 /usr/local/bin/bun /usr/local/bin/bun

# Install uv (Python package/dependency manager) the same way.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create a non-root user before the source tree is copied in, so COPY --chown
# below can set ownership at copy time instead of needing a separate `chown -R`
# afterward.
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid appuser --shell /bin/bash --create-home appuser

# Set the working directory
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"

# Install Python dependencies from pyproject.toml/uv.lock only, so this layer
# is cached independently of unrelated source changes below.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$UL_ENVIRONMENT" = "staging" ] || [ "$UL_ENVIRONMENT" = "production" ]; then \
        uv sync --frozen --no-install-project --no-dev; \
    else \
        uv sync --frozen --no-install-project; \
    fi

# Install JS/TS dependencies (sass, typescript, etc.) from the lockfile only,
# so this layer is cached independently of unrelated source changes below.
COPY package.json bun.lock ./
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

# Copy all source files into the container, setting ownership at copy time
# (avoids a slow recursive chown - see the useradd comment above).
COPY --chown=appuser:appuser . /app

# Install the package itself (editable) into the same venv
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$UL_ENVIRONMENT" = "staging" ] || [ "$UL_ENVIRONMENT" = "production" ]; then \
        uv sync --frozen --no-dev; \
    else \
        uv sync --frozen; \
    fi

# Pre-create every directory appuser writes to at runtime, so it never needs
# write access to root-owned parent dirs (volume mounts are handled separately
# by docker-entrypoint.sh):
#   - AppSettings.ensure_paths() dirs: /app/src/urbanlens/ (capital-U, legacy
#     runtime-data tree distinct from the lowercase source), /app/src/logs/,
#     /app/src/backups/
#   - bun build output: dashboard/frontend and core/frontend inside the source tree
#
# These are all gitignored (bun build output, logs, downloads, backups), so
# git never tracks them and they don't exist after COPY --chown above
RUN mkdir -p \
        /app/src/urbanlens/downloads/downloads \
        /app/src/urbanlens/downloads/exports \
        /app/src/urbanlens/frontend/static \
        /app/src/backups \
        /app/src/logs \
        /app/src/urbanlens/dashboard/frontend \
        /app/src/urbanlens/core/frontend && \
    touch \
        /app/src/logs/app.log \
        /app/src/logs/debugging.log \
        /app/src/logs/test.log && \
    chown -R appuser:appuser \
        /app/src/urbanlens/downloads \
        /app/src/urbanlens/frontend \
        /app/src/urbanlens/dashboard/frontend \
        /app/src/urbanlens/core/frontend \
        /app/src/backups \
        /app/src/logs

# Git >= 2.35.2 refuses to run in directories not owned by the current user.
# COPY . /app runs as root, so /app/.git is root-owned; the app runs as appuser.
# Writing to /etc/gitconfig (--system) applies the exception to all users.
RUN if [ "$UL_ENVIRONMENT" = "development" ] || [ "$UL_ENVIRONMENT" = "local" ]; then \
        git config --system safe.directory /app; \
    fi

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Entrypoint fixes volume-mount ownership then drops to appuser via gosu
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "/app/src/bin/init.py"]
