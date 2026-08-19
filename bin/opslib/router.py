"""Host-header router for ephemeral dev environments.

The alternative was an Nginx Proxy Manager entry per environment, which makes
creating one a two-system operation and leaves orphaned entries behind when
cleanup is forgotten. Instead NPM gets a single wildcard entry, once:

    *.dev.urbanlens.org  ->  chiron:21700

and this nginx container - which these scripts own outright - matches the Host
header and forwards to whichever port that environment was allocated. Creating
or destroying an environment rewrites one config file and reloads; NPM never
hears about it.

The one-time setup NPM still needs is deliberately small and documented in
``docs/DEV_ENVIRONMENTS.md``: a wildcard DNS record and a wildcard certificate.
Both are per-domain, not per-environment.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

#: Where the generated nginx config lives. Bind-mounted into the router.
ROUTER_DIR = Path(os.getenv("UL_DEV_ROUTER_DIR", "/projects/environments/agents/.router"))

#: The container name and the port NPM forwards to.
ROUTER_CONTAINER = os.getenv("UL_DEV_ROUTER_CONTAINER", "urbanlens_dev_router")
ROUTER_PORT = int(os.getenv("UL_DEV_ROUTER_PORT", "21700"))

_SERVER_TEMPLATE = """
server {{
    listen 80;
    server_name {hostname};

    # Long timeouts: a dev environment's first request often lands while the
    # app is still warming, and a 502 there reads as "broken" rather than
    # "not ready yet".
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    client_max_body_size 512m;

    location / {{
        proxy_pass http://host.docker.internal:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}
}}
"""

#: Answers anything that does not match a live environment. Without it nginx
#: serves the first server block to unmatched hosts, so a destroyed
#: environment's URL would quietly show somebody else's environment.
_DEFAULT_SERVER = """
server {
    listen 80 default_server;
    server_name _;
    return 404 "No such dev environment.\\n";
}
"""


def render_config(entries: dict[str, dict[str, Any]]) -> str:
    """Build the router's nginx config from the environment registry.

    Args:
        entries: The registry, keyed by slug.

    Returns:
        The complete config text.
    """
    blocks = [_DEFAULT_SERVER]
    for slug in sorted(entries):
        entry = entries[slug]
        hostname, port = entry.get("hostname"), entry.get("app_port")
        if not hostname or not port:
            continue
        blocks.append(_SERVER_TEMPLATE.format(hostname=hostname, port=int(port)))
    return "\n".join(blocks)


def write_routes(entries: dict[str, dict[str, Any]]) -> bool:
    """Regenerate the router config and apply it.

    Args:
        entries: The registry, keyed by slug.

    Returns:
        True when the config was written *and* applied. A create that could not
        publish its route still reports the environment, because the stack is
        usable on its port either way - the caller sees a warn, not a failure.
    """
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    (ROUTER_DIR / "envs.conf").write_text(render_config(entries), encoding="utf-8")
    return reload_router()


def reload_router(*, settle_seconds: float = 2.0) -> bool:
    """Validate and reload the router's config, waiting for it to take effect.

    ``nginx -s reload`` only *signals* the master process; it returns before the
    new workers are serving. A caller that requests a route immediately
    afterwards can be answered by a worker still holding the previous config,
    which looks exactly like the route having failed to publish. So the config
    is validated first (a reload with a broken config leaves nginx serving the
    old one silently), then signalled, then given a moment to settle.

    Args:
        settle_seconds: How long to wait after signalling before reporting
            success.

    Returns:
        True when the config validated and the reload was signalled; False when
        the container is absent or the config was rejected.
    """
    import time

    try:
        validated = subprocess.run(["docker", "exec", ROUTER_CONTAINER, "nginx", "-t"], capture_output=True, text=True, timeout=60, check=False)
        if validated.returncode != 0:
            return False
        reloaded = subprocess.run(["docker", "exec", ROUTER_CONTAINER, "nginx", "-s", "reload"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if reloaded.returncode != 0:
        return False
    time.sleep(settle_seconds)
    return True


def ensure_router() -> dict[str, Any]:
    """Start the router container if it is not already running.

    Idempotent: creating an environment calls this, so the first environment on
    a fresh host brings the router up without a separate setup step.

    Returns:
        A summary of what was done.
    """
    ROUTER_DIR.mkdir(parents=True, exist_ok=True)
    if not (ROUTER_DIR / "envs.conf").is_file():
        (ROUTER_DIR / "envs.conf").write_text(_DEFAULT_SERVER, encoding="utf-8")

    running = subprocess.run(["docker", "ps", "--filter", f"name=^{ROUTER_CONTAINER}$", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=60, check=False)
    if ROUTER_CONTAINER in running.stdout:
        return {"router": "already running", "port": ROUTER_PORT}

    subprocess.run(["docker", "rm", "-f", ROUTER_CONTAINER], capture_output=True, text=True, timeout=60, check=False)
    created = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", ROUTER_CONTAINER,
            "--restart", "unless-stopped",
            "-p", f"{ROUTER_PORT}:80",
            # Lets the proxied upstreams be host ports rather than container
            # networks, so the router does not need to join every environment's
            # compose network as it is created.
            "--add-host", "host.docker.internal:host-gateway",
            "-v", f"{ROUTER_DIR}:/etc/nginx/conf.d:ro",
            "nginx:alpine",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return {"router": "started" if created.returncode == 0 else "failed", "port": ROUTER_PORT, "detail": (created.stderr or "").strip()[:400]}
