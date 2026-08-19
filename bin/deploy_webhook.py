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
.env - see .env-sample). If a variable isn't already set in the process
environment, it's read directly from the repo's .env file as a fallback -
useful when running this script by hand outside of whatever normally
injects .env (systemd EnvironmentFile, docker compose, etc.):
    UL_DEPLOY_WEBHOOK_SECRET  Shared secret configured on the Git host's webhook. Required.
    UL_DEPLOY_WEBHOOK_BRANCH  Branch this host deploys on push, e.g. "staging". Required.
    UL_DEPLOY_WEBHOOK_HOST    Bind address. Default: 127.0.0.1
    UL_DEPLOY_WEBHOOK_PORT    Bind port. Default: 9000
    UL_DEPLOY_WEBHOOK_PATH    URL path the Git host POSTs to. Default: /webhook
    UL_DEPLOY_LOG_FILE        Log file path. Default: ./deploy_webhook.log
    UL_OPS_TOKEN              Bearer token for the staging pipeline endpoints
                              below. Unset disables them entirely.
    UL_PROD_DIR               Production checkout the staging pipeline copies
                              data from. Unset skips the data clone.

Beyond the git webhook, three endpoints let a caller drive and observe a full
staging deploy without shell access. All three need `Authorization: Bearer
$UL_OPS_TOKEN`:

    POST /staging/deploy[?wait=1][&branch=X][&skip_data=1][&skip_tests=1]
        Runs pull -> clone prod data -> rebuild -> wait healthy -> verify
        migrations, row counts and pages -> integration tests. Returns the run
        id immediately, or the whole record when `wait=1`.
    GET  /staging/runs/<run_id>       The run record as JSON.
    GET  /staging/runs/<run_id>/log   The transcript, for when a step failed.

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
import threading

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "bin" / "deploy.sh"


def _load_dotenv_fallback(dotenv_path: Path) -> None:
    """Fill in missing os.environ entries from a .env file.

    Only stdlib is available here (see module docstring), so this is a
    minimal KEY=VALUE parser rather than python-dotenv. Real environment
    variables always win - this only covers vars the process wasn't
    launched with (e.g. running the script by hand outside of whatever
    normally injects .env into the environment).
    """
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv_fallback(REPO_ROOT / ".env")

SECRET = os.environ.get("UL_DEPLOY_WEBHOOK_SECRET", "")
BRANCH = os.environ.get("UL_DEPLOY_WEBHOOK_BRANCH", "")
HOST = os.environ.get("UL_DEPLOY_WEBHOOK_HOST", "127.0.0.1")
PORT = int(os.environ.get("UL_DEPLOY_WEBHOOK_PORT", "9000"))
WEBHOOK_PATH = os.environ.get("UL_DEPLOY_WEBHOOK_PATH", "/webhook")
OPS_TOKEN = os.environ.get("UL_OPS_TOKEN", "")
PROD_DIR = os.environ.get("UL_PROD_DIR", "")

#: Runs started through this server, by id. Bounded because this process is
#: long-lived: an unbounded dict here is a slow leak that only shows up on a
#: host nobody restarts.
_RUNS: dict[str, dict] = {}
_RUNS_LIMIT = 50
_RUNS_LOCK = threading.Lock()
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

    def _handle_staging_deploy(self) -> None:
        """Start a staging deploy, optionally blocking until it finishes.

        ``wait=1`` holds the connection open for the whole run - which is the
        point: a caller gets one pass/fail answer without polling, and only
        goes reading logs when the answer is "fail". Without it the run id
        comes back immediately and the GET endpoints report progress.
        """
        if not self._authorized_for_ops():
            self._respond(403, "forbidden")
            return

        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        wait = query.get("wait", ["0"])[0] not in ("0", "", "false")
        branch = query.get("branch", [BRANCH])[0]
        skip_data = query.get("skip_data", ["0"])[0] not in ("0", "", "false")
        skip_tests = query.get("skip_tests", ["0"])[0] not in ("0", "", "false")

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from opslib.staging import run_staging_pipeline

        checkout = Path(__file__).resolve().parent.parent

        # Named here, not inside the pipeline: the async branch below returns
        # this id to the caller, and an id that does not match the run it
        # started is an id that 404s forever.
        run_id = f"staging-deploy-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

        def _execute() -> dict:
            record = run_staging_pipeline(
                checkout=checkout,
                branch=branch,
                prod_dir=Path(PROD_DIR) if PROD_DIR and not skip_data else None,
                run_dir=checkout / ".ops-runs",
                skip_data=skip_data,
                skip_tests=skip_tests,
                run_id=run_id,
            )
            with _RUNS_LOCK:
                _RUNS[record["run_id"]] = record
                # Oldest first, so a long-lived server keeps the recent runs a
                # caller might still be asking about.
                for stale in sorted(_RUNS)[:-_RUNS_LIMIT]:
                    _RUNS.pop(stale, None)
            return record

        if wait:
            log(f"Staging deploy (blocking) branch={branch}")
            self._respond_json(200 if (record := _execute())["status"] == "ok" else 500, record)
            return

        log(f"Staging deploy (async) branch={branch} run_id={run_id}")
        # Registered before the thread starts, so a poll that arrives while the
        # deploy is still running is answered "running" rather than "unknown".
        with _RUNS_LOCK:
            _RUNS[run_id] = {"run_id": run_id, "status": "running", "steps": [], "log_path": str(checkout / ".ops-runs" / f"{run_id}.log")}
        threading.Thread(target=_execute, daemon=True).start()
        self._respond_json(202, {"run_id": run_id, "status": "started", "poll": f"/staging/runs/{run_id}"})

    def _authorized_for_ops(self) -> bool:
        """Whether this request carries the ops bearer token.

        Compared with ``compare_digest`` for the same reason the webhook
        signatures are: a plain ``==`` on a secret leaks its prefix through
        timing.
        """
        if not OPS_TOKEN:
            return False
        offered = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(offered) and hmac.compare_digest(offered, OPS_TOKEN)

    def _respond_json(self, status: int, payload: dict) -> None:
        """Send a JSON body."""
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._respond(200, "ok")
            return

        if self.path.startswith("/staging/runs/"):
            if not self._authorized_for_ops():
                self._respond(403, "forbidden")
                return
            run_id = self.path.removeprefix("/staging/runs/").removesuffix("/log")
            with _RUNS_LOCK:
                record = _RUNS.get(run_id)
            if record is None:
                self._respond_json(404, {"error": "unknown run", "run_id": run_id})
                return
            if self.path.endswith("/log"):
                log_path = Path(record.get("log_path", ""))
                self._respond(200, log_path.read_text(encoding="utf-8") if log_path.is_file() else "(log not found)")
                return
            self._respond_json(200, record)
            return

        self._respond(404, "not found")

    def do_POST(self) -> None:
        if self.path.split("?")[0] == "/staging/deploy":
            self._handle_staging_deploy()
            return

        if self.path != WEBHOOK_PATH:
            self._respond(404, "not found")
            return

        if not SECRET or not BRANCH:
            log("Rejecting webhook: UL_DEPLOY_WEBHOOK_SECRET / UL_DEPLOY_WEBHOOK_BRANCH not configured.")
            self._respond(500, "server not configured")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""

        authorized = verify_github(body, self.headers.get("X-Hub-Signature-256")) or verify_gitlab(self.headers.get("X-Gitlab-Token"))
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
        pushed_branch = ref[len(prefix) :] if ref.startswith(prefix) else ""
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
