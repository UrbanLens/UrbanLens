# Allow future upgrades by pinning the base image version here
ARG PYTHON_BASE_IMAGE_VERSION=3.12-bookworm

FROM python:${PYTHON_BASE_IMAGE_VERSION} AS base

# Controls which requirements file gets installed below. staging/production
# install only prod.txt (no linters/test tools/debug toolbar); everything
# else (local, development, testing) installs dev.txt, which pulls in prod.txt
# via -r plus the dev-only tooling.
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

# Create a non-root user before the source tree is copied in, so COPY --chown
# below can set ownership at copy time instead of needing a separate `chown -R`
# afterward. 
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid appuser --shell /bin/bash --create-home appuser

# Handle Python requirements
COPY requirements /tmp/pip-tmp/requirements/
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$UL_ENVIRONMENT" = "staging" ] || [ "$UL_ENVIRONMENT" = "production" ]; then \
        pip install -r /tmp/pip-tmp/requirements/prod.txt; \
    else \
        pip install -r /tmp/pip-tmp/requirements/dev.txt; \
    fi

# Set the working directory
WORKDIR /app

# Install JS/TS dependencies (sass, typescript, etc.) from the lockfile only,
# so this layer is cached independently of unrelated source changes below.
COPY package.json bun.lock ./
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install --frozen-lockfile

# Copy all source files into the container, setting ownership at copy time
# (avoids a slow recursive chown - see the useradd comment above).
COPY --chown=appuser:appuser . /app

# Install the package in editable mode
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e .

# Pre-create every directory appuser writes to at runtime, so it never needs
# write access to root-owned parent dirs (volume mounts are handled separately
# by docker-entrypoint.sh):
#   - AppSettings.ensure_paths() dirs: /app/src/urbanlens/ (capital-U, legacy
#     runtime-data tree distinct from the lowercase source), /app/src/logs/,
#     /app/src/backups/
#   - bun build output: dashboard/frontend and core/frontend inside the source tree
#
# Most of these paths already exist in the source tree and were already given
# to appuser by the COPY --chown above, so mkdir -p is a no-op for them; only
# backups/ and the downloads/ subfolders (gitignored) are genuinely new here,
# so only those need an explicit chown.
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
    chown -R appuser:appuser /app/src/urbanlens/downloads /app/src/backups /app/src/urbanlens/**/frontend/static

# Git >= 2.35.2 refuses to run in directories not owned by the current user.
# COPY . /app runs as root, so /app/.git is root-owned; the app runs as appuser.
# Writing to /etc/gitconfig (--system) applies the exception to all users.
RUN git config --system safe.directory /app

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Entrypoint fixes volume-mount ownership then drops to appuser via gosu
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "/app/src/bin/init.py"]
