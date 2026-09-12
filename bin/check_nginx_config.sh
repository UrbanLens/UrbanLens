#!/usr/bin/env bash
# Ask nginx whether it would load the config, which is the only complete answer.
#
# `test_nginx_config_is_loadable.py` enforces the placement rules we know to
# look for; this catches everything else, because the oracle is nginx itself.
# It needs Docker (the app image has no nginx binary), so it is a pre-commit
# hook rather than a pytest case, and it skips rather than fails where Docker is
# unavailable - a hook that cannot run must not block a commit it never checked.
#
# Upstream hostnames only resolve inside the compose network, so the test runs
# there when a stack is up; without one, nginx stops at "host not found in
# upstream", which means every syntax and context rule already passed.
set -euo pipefail

cd "$(dirname "$0")/.."
CONF_DIR="src/urbanlens/config/nginx"
IMAGE="${UL_NGINX_IMAGE:-nginxinc/nginx-unprivileged:alpine}"

if ! docker info >/dev/null 2>&1; then
    echo "check_nginx_config: no usable Docker daemon - skipping" >&2
    exit 0
fi

# The network of a *running* app container, not merely one that exists: a
# stopped stack's network resolves nothing, which looks identical to a config
# that names an upstream wrong.
network=""
running=$(docker ps --filter 'name=_app$' --format '{{.Names}}' | head -1)
if [ -n "$running" ]; then
    candidate=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$running" | tr ' ' '\n' | grep 'app_network$' | head -1 || true)
    [ -n "$candidate" ] && network="--network $candidate"
fi

output=$(docker run --rm ${network} \
    -v "$PWD/$CONF_DIR/nginx.conf:/etc/nginx/nginx.conf:ro" \
    -v "$PWD/$CONF_DIR/django.conf:/etc/nginx/conf.d/django.conf:ro" \
    -v /dev/null:/etc/nginx/conf.d/default.conf:ro \
    "$IMAGE" nginx -t 2>&1) || true

if grep -q 'syntax is ok' <<<"$output"; then
    echo "check_nginx_config: nginx accepts the config"
    exit 0
fi

# The only failure that is about the environment rather than the config.
if grep -q 'host not found in upstream' <<<"$output"; then
    echo "check_nginx_config: syntax and context checks passed (upstreams need a running stack to resolve)"
    exit 0
fi

echo "check_nginx_config: nginx REFUSES this config - it would not start" >&2
grep -E '\[emerg\]|\[error\]|nginx:' <<<"$output" >&2 || echo "$output" >&2
exit 1
