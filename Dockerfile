# Allow future upgrades by pinning the base image version here
ARG PYTHON_BASE_IMAGE_VERSION=3.12-bookworm

FROM python:${PYTHON_BASE_IMAGE_VERSION} AS base

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
    locale-gen en_US.UTF-8 && \
    update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 && \
    pg_dump --version && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Now safe to set globally, since the locale exists on disk
ENV LANG=en_US.UTF-8 \
LANGUAGE=en_US.UTF-8 \
LC_ALL=en_US.UTF-8 \
LC_CTYPE=en_US.UTF-8

# Install Bun (JS runtime + package manager) via the official Docker image.
# This avoids the curl-to-bash NVM install and gives a reproducible binary.
COPY --from=oven/bun:1 /usr/local/bin/bun /usr/local/bin/bun

# Handle Python requirements
COPY requirements /tmp/pip-tmp/requirements/
RUN pip --no-cache-dir install -r /tmp/pip-tmp/requirements/dev.txt

# Copy all source files into the container
COPY . /app

# Set the working directory
WORKDIR /app

# Install the package in editable mode
RUN pip --no-cache-dir install -e .

# Install JS/TS dependencies (sass, typescript, etc.)
RUN bun install

# Create a non-root user. Pre-create every directory written to at runtime so
# appuser never needs write access to root-owned parent dirs.
#
# Three categories handled here (volume mounts are handled by docker-entrypoint.sh):
#   - AppSettings.ensure_paths() dirs: /app/src/urbanlens/ (capital-U, legacy
#     runtime-data tree distinct from the lowercase source), /app/src/logs/,
#     /app/src/backups/
#   - bun build output: dashboard/frontend and core/frontend inside the source tree
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid appuser --shell /bin/bash --create-home appuser && \
    mkdir -p \
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
        /app/src/urbanlens \
        /app/src/backups \
        /app/src/logs \
        /app/src/urbanlens/dashboard/frontend \
        /app/src/urbanlens/core/frontend

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Entrypoint fixes volume-mount ownership then drops to appuser via gosu
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "/app/src/bin/init.py"]
