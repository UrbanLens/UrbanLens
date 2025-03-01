################################################################################
#                                                                              #
# Metadata:                                                                    #
#                                                                              #
# 	File: Dockerfile                                                           #
# 	Project: src                                                               #
#
# 	Author: Jess Mann                                                          #
# 	Email: jess@urbanlens.org                                                    #
#                                                                              #
# 	-----                                                                      #
#                                                                              #
#
# 	Modified By: Jess Mann                                                     #
#                                                                              #
# 	-----                                                                      #
#                                                                              #
# 	Copyright (c) 2023 Urban Lens                                               #
################################################################################

# Allow future upgrades
ARG PYTHON_BASE_IMAGE_VERSION=0-3.12
ARG GIT_EMAIL
ARG GIT_NAME
ARG GH_TOKEN
ARG SSH_PRIVATE_KEY
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
	GIT_EMAIL=${GIT_EMAIL} \
	GIT_NAME=${GIT_NAME} \
	UL_DATABASE_HOST=${UL_DATABASE_HOST} \
	UL_DATABASE_PORT=${UL_DATABASE_PORT} \
	UL_DATABASE_NAME=${UL_DATABASE_NAME} \
	UL_DATABASE_USER=${UL_DATABASE_USER} \
	UL_DATABASE_PASS=${UL_DATABASE_PASS} \
	NODE_ENV=${ENVIRONMENT}

# Set Git config
RUN if [ -n "$GIT_EMAIL" ]; then \
	git config --global user.email "${GIT_EMAIL}"; \
	fi
RUN if [ -n "$GIT_NAME" ]; then \
	git config --global user.name "${GIT_NAME}"; \
	fi

# Add SSH keys based on build args
RUN if [ -n "$SSH_PRIVATE_KEY" ]; then \
	mkdir -p /root/.ssh/ && \
	echo "${SSH_PRIVATE_KEY}" > /root/.ssh/id_rsa && \
	chmod 600 /root/.ssh/id_rsa && \
	ssh-keyscan github.com >> /root/.ssh/known_hosts; \
	fi

# Add Github cli repo
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
	&& sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
	&& echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null

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
	wget && \
	apt-get clean && \
	rm -rf /var/lib/apt/lists/*

# Install npm
RUN curl -sL https://deb.nodesource.com/setup_20.x | sudo -E bash - && \
	apt-get install -y nodejs

# Handle Python requirements
COPY requirements /tmp/pip-tmp/requirements/
RUN pip --no-cache-dir install -r /tmp/pip-tmp/requirements/dev.txt

# Copy init.py into the container
COPY src/bin/init.py /usr/local/bin/urbanlens_init.py

# Copy all source files into the container
COPY . /app

# Set the working directory
WORKDIR /app

# Install npm packages
RUN npm install -y

#ENTRYPOINT ["gunicorn", "UrbanLens.wsgi:application", "--bind", "0.0.0.0:8000", "-t", "600", "-k", "gevent"]
ENTRYPOINT ["python", "/usr/local/bin/urbanlens_init.py"]