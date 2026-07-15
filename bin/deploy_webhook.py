#!/usr/bin/env python3
"""Minimal HTTP listener that triggers bin/deploy.sh on a verified push webhook.

Runs as a standalone host process (see bin/systemd/urbanlens-deploy-webhook.service)
outside of docker compose - it has to survive `docker compose down`, which rules
out running it as one of the services it's restarting. Deliberately stdlib-only
so it doesn't need the project venv or any dependency installation on the host;
it never imports Django or anything under src/urbanlens, it only verifies a
webhook signature and shells out to bin/deploy.sh.

Supports GitHub's HMAC-SHA256 signature scheme (X-Hub-Signature-256) and
GitLab's shared-token scheme (X-Gitlab-Token) - whichever header is present is
checked; requests with neither are rejected.

Configuration is read from the environment (populate these in the checkout's
.env - see .env-sample):
    UL_DEPLOY_WEBHOOK_SECRET  Shared secret configured on the Git host's webhook. Required.
    UL_DEPLOY_WEBHOOK_BRANCH  Branch this host deploys on push, e.g. "staging". Required.
    UL_DEPLOY_WEBHOOK_HOST    Bind address. Default: 127.0.0.1
    UL_DEPLOY_WEBHOOK_PORT    Bind port. Default: 9000
    UL_DEPLOY_WEBHOOK_PATH    URL path the Git host POSTs to. Default: /webhook
    UL_DEPLOY_LOG_FILE        Log file path. Default: ./deploy_webhook.log

Usage:
    python3 bin/deploy_webhook.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "bin" / "deploy.sh"

SECRET = os.environ.get("UL_DEPLOY_WEBHOOK_SECRET", "")
BRANCH = os.environ.get("UL_DEPLOY_WEBHOOK_BRANCH", "")
HOST = os.environ.get("UL_DEPLOY_WEBHOOK_HOST", "127.0.0.1")
PORT = int(os.environ.get("UL_DEPLOY_WEBHOOK_PORT", "9000"))
WEBHOOK_PATH = os.environ.get("UL_DEPLOY_WEBHOOK_PATH", "/webhook")
LOG_FILE = Path(os.environ.get("UL_DEPLOY_LOG_FILE", REPO_ROOT / "deploy_webhook.log"))


def log(message: str) -> None:
    """Write a timestamped line to stdout (systemd journal) and the log file."""
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def verify_github(body: bytes, header_sig: str | None) -> bool:
    """Check GitHub's X-Hub-Signature-256 HMAC over the raw request body."""
    if not header_sig or not header_sig.startswith("sha256="):
        return False
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig.removeprefix("sha256="))


def verify_gitlab(header_token: str | None) -> bool:
    """Check GitLab's X-Gitlab-Token shared-secret header."""
    if not header_token:
        return False
    return hmac.compare_digest(SECRET, header_token)


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "UrbanLensDeployWebhook/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        # Route the default per-request access line through our own logger
        # instead of stderr, so it lands in LOG_FILE alongside deploy events.
        log(f"{self.address_string()} {format % args}")

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, "ok")
            return
        self._respond(404, "not found")

    def do_POST(self) -> None:
        if self.path != WEBHOOK_PATH:
            self._respond(404, "not found")
            return

        if not SECRET or not BRANCH:
            log("Rejecting webhook: UL_DEPLOY_WEBHOOK_SECRET / UL_DEPLOY_WEBHOOK_BRANCH not configured.")
            self._respond(500, "server not configured")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""

        authorized = verify_github(body, self.headers.get("X-Hub-Signature-256")) or verify_gitlab(
            self.headers.get("X-Gitlab-Token")
        )
        if not authorized:
            log("Rejecting webhook: missing or invalid signature.")
            self._respond(403, "invalid signature")
            return

        event = self.headers.get("X-GitHub-Event") or self.headers.get("X-Gitlab-Event") or ""
        if event.lower() == "ping":
            self._respond(200, "pong")
            return

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._respond(400, "invalid JSON body")
            return

        # Strip the fixed "refs/heads/" prefix rather than splitting on "/" -
        # branch names routinely contain slashes themselves (e.g. "feature/x"),
        # which a rsplit("/", 1) would truncate down to just the last segment.
        ref = payload.get("ref", "")
        prefix = "refs/heads/"
        pushed_branch = ref[len(prefix):] if ref.startswith(prefix) else ""
        if pushed_branch != BRANCH:
            self._respond(200, f"ignored: push to '{pushed_branch or 'unknown'}', watching '{BRANCH}'")
            return

        log(f"Verified push to '{BRANCH}' ({payload.get('after', '?')[:12]}) - triggering deploy.")
        try:
            with LOG_FILE.open("a", encoding="utf-8") as log_fd:
                subprocess.Popen(
                    ["/bin/bash", str(DEPLOY_SCRIPT), BRANCH],
                    cwd=REPO_ROOT,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            log(f"Failed to launch deploy script: {exc}")
            self._respond(500, "failed to launch deploy script")
            return
        self._respond(202, "deploy triggered")


def main() -> None:
    if not SECRET:
        sys.exit("UL_DEPLOY_WEBHOOK_SECRET is not set - refusing to start unauthenticated.")
    if not BRANCH:
        sys.exit("UL_DEPLOY_WEBHOOK_BRANCH is not set - don't know which branch to deploy on push.")
    if not DEPLOY_SCRIPT.exists():
        sys.exit(f"Deploy script not found: {DEPLOY_SCRIPT}")

    server = ThreadingHTTPServer((HOST, PORT), WebhookHandler)
    log(f"Listening on {HOST}:{PORT}{WEBHOOK_PATH}, deploying branch '{BRANCH}' via {DEPLOY_SCRIPT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
