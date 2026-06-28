# Allow future upgrades
ARG PYTHON_BASE_IMAGE_VERSION=3.12-bookworm
ARG UL_DATABASE_NAME
ARG UL_DATABASE_USER
ARG UL_DATABASE_PASS
ARG UL_DATABASE_HOST
ARG UL_DATABASE_PORT
ARG ENVIRONMENT

# AppServer image
FROM mcr.microsoft.com/devcontainers/python:${PYTHON_BASE_IMAGE_VERSION} AS base

# Ensure logging dir exists at /var/log/urbanlens
RUN mkdir -p /var/log/urbanlens

# Environment variables
# TODO: multi-stage build to hide env vars
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LC_CTYPE=en_US.UTF-8 \
    UL_DATABASE_HOST=${UL_DATABASE_HOST} \
    UL_DATABASE_PORT=${UL_DATABASE_PORT} \
    UL_DATABASE_NAME=${UL_DATABASE_NAME} \
    UL_DATABASE_USER=${UL_DATABASE_USER} \
    UL_DATABASE_PASS=${UL_DATABASE_PASS} \
    NODE_ENV=${ENVIRONMENT} \
    PYTHONPATH=/app/src

# Dependencies for building packages
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive && \
    apt-get install -y --no-install-recommends \
    curl gcc vim pkg-config \
    build-essential \
    unzip \
    postgresql-client \
    git \
    gh \
    iputils-ping \
    libgdal-dev \
    wget \
    gosu && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install npm
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && \
    export NVM_DIR="/usr/local/share/nvm" && \
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh" && \
    [ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion" && \
    nvm install node

# Handle Python requirements
COPY requirements /tmp/pip-tmp/requirements/
RUN pip --no-cache-dir install -r /tmp/pip-tmp/requirements/dev.txt

# Copy all source files into the container
COPY . /app

# Set the working directory
WORKDIR /app

# Install the package in editable mode
RUN pip install -e .

# Install npm packages
RUN npm install -y

# Create a non-root user. Pre-create every directory written to at runtime so
# appuser never needs write access to root-owned parent dirs.
#
# Three categories handled here (volume mounts are handled by docker-entrypoint.sh):
#   - AppSettings.ensure_paths() dirs: /app/src/urbanlens/ (capital-U, legacy
#     runtime-data tree distinct from the lowercase source), /app/src/logs/,
#     /app/src/backups/
#   - npm build output: dashboard/frontend and core/frontend inside the source tree
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
