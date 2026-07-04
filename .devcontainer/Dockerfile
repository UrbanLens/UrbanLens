# Devcontainer-only image. Keep the root Dockerfile production-focused; this
# image starts from Microsoft's Python devcontainer base for editor/terminal
# ergonomics, then installs the project runtime dependencies needed for local
# development.
ARG PYTHON_BASE_IMAGE_VERSION=3.12-bookworm
FROM mcr.microsoft.com/devcontainers/python:${PYTHON_BASE_IMAGE_VERSION}

ARG POSTGRES_MAJOR=17
ARG GIT_EMAIL
ARG GIT_NAME

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    UL_ENVIRONMENT=development \
    NODE_ENV=development \
    UL_DB_HOST=urbanlens_db \
    UL_DB_PORT=5432 \
    UL_DB_NAME=postgres \
    UL_DB_USER=postgres \
    UL_DB_PASS=postgres \
    UL_VALKEY_URL=redis://urbanlens_valkey:6379/0 \
    UL_PG_DUMP_BIN=/usr/lib/postgresql/17/bin/pg_dump \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LC_CTYPE=en_US.UTF-8

# Match the project runtime dependencies that matter in development, including
# the PostgreSQL client major used by production, while adding interactive tools
# that should not be baked into the production image.
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive && \
    apt-get install -y --no-install-recommends ca-certificates curl gcc pkg-config gnupg && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc && \
    . /etc/os-release && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        bash-completion \
        build-essential \
        gh \
        git \
        iputils-ping \
        jq \
        less \
        libgdal-dev \
        locales \
        lsof \
        nano \
        netcat-openbsd \
        openssh-client \
        postgresql-client-${POSTGRES_MAJOR} \
        procps \
        rsync \
        tree \
        unzip \
        vim \
        wget \
        zsh && \
    sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && \
    locale-gen && \
    pg_dump --version && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Use the same reproducible Bun and uv binary sources as the production Dockerfile.
COPY --from=oven/bun:1 /usr/local/bin/bun /usr/local/bin/bun
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install Python dependencies (including the `dev` group) from pyproject.toml/uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

RUN mkdir -p \
        /app/src/urbanlens/downloads/downloads \
        /app/src/urbanlens/downloads/exports \
        /app/src/urbanlens/frontend/static \
        /app/src/backups \
        /app/src/logs \
        /app/src/urbanlens/dashboard/frontend \
        /app/src/urbanlens/core/frontend && \
    chown -R vscode:vscode /app

RUN if [ -n "$GIT_EMAIL" ]; then git config --global user.email "${GIT_EMAIL}"; fi && \
    if [ -n "$GIT_NAME" ]; then git config --global user.name "${GIT_NAME}"; fi

USER vscode
