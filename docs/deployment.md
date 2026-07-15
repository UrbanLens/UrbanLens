# Automated deploys (staging / dev)

Staging and dev redeploy automatically when the tracked branch is pushed,
instead of someone ssh-ing in to run:

```bash
docker compose down && git pull && docker compose up --build
```

## Why a webhook instead of a poller

GitHub/GitLab already emit an HTTP POST the instant a push happens, signed with
a shared secret. A small listener that verifies that signature and shells out
to the same commands you'd otherwise type over ssh gets you an instant,
event-driven deploy with no wasted requests and nothing to tune (a poller has
to pick an interval, is always at least one interval "late", and still hits
the Git host on every tick whether or not anything changed).

This is implemented as two pieces:

- **`bin/deploy.sh`** - the actual deploy. Fetches the target branch, refuses
  to run if the checkout is dirty or on the wrong branch, no-ops if there's
  nothing new, otherwise does `git reset --hard origin/<branch>` followed by
  `docker compose down` + `docker compose up --build -d`. Locks itself with
  `flock` so two triggers (e.g. a retried webhook delivery) can't race.
- **`bin/deploy_webhook.py`** - a stdlib-only HTTP listener that verifies the
  webhook signature and, on a push to the configured branch, runs
  `bin/deploy.sh` in the background. It runs as a plain host process (not a
  docker compose service) specifically so that `docker compose down` doesn't
  take it down with everything else.

Other established options considered:

- **CI-driven deploy (GitHub Actions + SSH)** - a workflow SSHes into the
  server on push and runs the same commands. Centralizes config in the repo
  instead of per-host, but means a deploy key with server access lives in CI
  secrets, and every push consumes Actions minutes. Reasonable if you're
  already leaning on Actions elsewhere.
- **Watchtower** - polls a container registry for new image digests. Doesn't
  fit here since images are built locally from source (`docker compose up
  --build`) rather than pulled from a registry.
- **A `post-receive` git hook** - only applies if the server itself is the git
  remote you push to; this repo pushes to GitHub, so the server is always a
  downstream clone.

The webhook approach was chosen because it needs no extra service dependency,
matches the existing pattern of host-side scripts in `bin/`, and keeps
deploy credentials out of CI.

## Setup

1. **Add config to the checkout's `.env`** (see `.env-sample`):

   ```
   UL_DEPLOY_WEBHOOK_SECRET=<a long random value, e.g. `openssl rand -hex 32`>
   UL_DEPLOY_WEBHOOK_BRANCH=staging   # or "development", etc - whatever this host tracks
   ```

   `UL_DEPLOY_WEBHOOK_HOST`/`UL_DEPLOY_WEBHOOK_PORT` only need setting if you
   want something other than the localhost-only default (`127.0.0.1:9000`).

2. **Make the scripts executable** (once, on the server):

   ```bash
   chmod +x bin/deploy.sh bin/deploy_webhook.py
   ```

3. **Install the systemd unit.** Copy
   `bin/systemd/urbanlens-deploy-webhook.service` to
   `/etc/systemd/system/`, edit its `WorkingDirectory`/`EnvironmentFile`
   paths and `User`/`Group` for this host (the user needs to be in the
   `docker` group so `docker compose` works without `sudo`), then:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now urbanlens-deploy-webhook
   sudo systemctl status urbanlens-deploy-webhook
   ```

   If you run both a staging and a dev listener on the same host, copy the
   unit file under two different names (e.g.
   `urbanlens-deploy-webhook-staging.service`) with distinct
   `WorkingDirectory`/port values, since each tracks a different checkout.

4. **Expose the listener to the Git host.** `UL_DEPLOY_WEBHOOK_HOST` defaults
   to `127.0.0.1`, so by default nothing outside the machine can reach it -
   you need to deliberately open a path in. Two options:
   - Point a location block on whatever reverse proxy already terminates TLS
     for the host (this repo's own nginx runs inside docker compose and only
     proxies the app, so it doesn't cover this) at
     `http://127.0.0.1:9000/webhook`.
   - Or set `UL_DEPLOY_WEBHOOK_HOST=0.0.0.0` and open the port directly in
     the host firewall, ideally restricted to
     [GitHub's published webhook IP ranges](https://api.github.com/meta)
     (`hooks` key) as defense in depth on top of signature verification.

5. **Add the webhook on GitHub**: repo Settings → Webhooks → Add webhook.
   - Payload URL: `https://<host>/webhook` (or whatever path you proxied to).
   - Content type: `application/json`.
   - Secret: the same value as `UL_DEPLOY_WEBHOOK_SECRET`.
   - Events: "Just the push event".

   GitLab: Settings → Webhooks. Use the "Secret token" field (compared
   directly, not HMAC'd) instead of a signing secret, and select "Push events".

## Logs

Both scripts append to `deploy_webhook.log` / `deploy.log` in the checkout
root (paths overridable via `UL_DEPLOY_LOG_FILE`), and the listener's stdout
also goes to the systemd journal (`journalctl -u urbanlens-deploy-webhook -f`).

## Production

This is intentionally not wired up for production - production deploys should
stay a deliberate, manual action.
