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
	wget && \
	apt-get clean && \
	rm -rf /var/lib/apt/lists/*

# Install npm
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && \
	nvm install node

# Handle Python requirements
COPY requirements /tmp/pip-tmp/requirements/
RUN pip --no-cache-dir install -r /tmp/pip-tmp/requirements/dev.txt

# Copy init.py into the container
COPY src/bin/init.py /usr/local/bin/urbanlens_init.py

# Copy all source files into the container
COPY . /app

# Set the working directory
WORKDIR /app

# Install the package in editable mode
RUN pip install -e .

# Install npm packages
RUN npm install -y

#ENTRYPOINT ["gunicorn", "UrbanLens.wsgi:application", "--bind", "0.0.0.0:8000", "-t", "600", "-k", "gevent"]
ENTRYPOINT ["python", "/usr/local/bin/urbanlens_init.py"]
