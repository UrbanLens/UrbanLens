# Automated deploys (staging / dev)

Staging and dev can redeploy automatically when the tracked branch is pushed,
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
  `bin/deploy.sh` in the background. It's a plain script - no service
  manager, no install step - so that `docker compose down` doesn't take it
  down with everything else, and so it's trivial to start/stop by hand.

Other established options considered and not used: CI-driven deploy (GitHub
Actions SSHing in) puts a deploy key with server access into CI secrets and
costs Actions minutes per push; Watchtower polls a container *registry*,
which doesn't fit since images here are built locally from source; a
`post-receive` git hook only works if the server itself is the git remote,
and this repo pushes to GitHub instead.

## Running it (manual, on demand)

This deliberately isn't a systemd service or anything else that installs
itself into the host - the TrueNAS box this runs on is shared with other
things, and host-level services there don't survive updates cleanly anyway.
Instead, start the listener by hand when you're actively working and want
pushes to auto-deploy, and stop it when you're done.

1. **Add config to the checkout's `.env`** (see `.env-sample`):

   ```
   UL_DEPLOY_WEBHOOK_SECRET=<a long random value, e.g. `openssl rand -hex 32`>
   UL_DEPLOY_WEBHOOK_BRANCH=main   # whatever branch *this checkout* tracks
   UL_DEPLOY_WEBHOOK_HOST=0.0.0.0  # or a specific LAN IP - see the NPM note below
   UL_DEPLOY_WEBHOOK_PORT=9123     # whatever port you point NPM at
   ```

   `UL_DEPLOY_WEBHOOK_BRANCH` is just "the branch this listener redeploys on
   push" - if this checkout is staging tracking `main`, use `main`; if you
   later add a second checkout for a `staging` branch, that one gets its own
   `.env` with `UL_DEPLOY_WEBHOOK_BRANCH=staging`.

2. **Make the scripts executable** (once):

   ```bash
   chmod +x bin/deploy.sh bin/deploy_webhook.py
   ```

3. **Start it** in a `tmux`/`screen` session (so it survives your ssh
   session ending) from inside the checkout:

   ```bash
   tmux new -s ul-deploy-webhook
   set -a; source .env; set +a
   python3 bin/deploy_webhook.py
   # Ctrl-b d to detach, leaving it running
   ```

   Or run it detached without tmux:

   ```bash
   set -a; source .env; set +a
   nohup python3 bin/deploy_webhook.py >> deploy_webhook_stdout.log 2>&1 &
   disown
   echo $! > .deploy_webhook.pid
   ```

   To stop it: reattach the tmux session and Ctrl-C, or
   `kill "$(cat .deploy_webhook.pid)"` for the nohup form.

   A `GET /healthz` on the configured host/port returns `200 ok` while it's up.


## Reaching it from GitHub

`deploy_webhook.py` binds directly (it's not in docker compose, and isn't
proxied by this repo's own nginx, which only routes to the app). To make it
reachable from the internet:

1. In **Nginx Proxy Manager**, add a Proxy Host: domain of your choice ->
   forward to the TrueNAS host's IP and `UL_DEPLOY_WEBHOOK_PORT`. NPM runs as
   its own app/container, so `127.0.0.1` won't reach a process running
   directly on the host - use the host's actual LAN IP as the forward target,
   and set `UL_DEPLOY_WEBHOOK_HOST=0.0.0.0` (or that same LAN IP) so the
   listener actually accepts the connection. Enable TLS on the NPM side.
2. In **GitHub**: repo Settings -> Webhooks -> Add webhook.
   - Payload URL: `https://<the NPM domain>/webhook`.
   - Content type: `application/json`.
   - Secret: the same value as `UL_DEPLOY_WEBHOOK_SECRET`.
   - Events: "Just the push event".

   GitLab: Settings -> Webhooks. Use the "Secret token" field (compared
   directly, not HMAC'd) instead of a signing secret, and select "Push events".

Since the port ends up reachable on the LAN (and, via NPM, the internet),
signature verification is the real access control here - `deploy_webhook.py`
rejects anything without a valid `X-Hub-Signature-256`/`X-Gitlab-Token`
before it looks at the payload at all. If your router/TrueNAS network setup
makes it easy, restricting the port to NPM's address is a reasonable extra
layer, but isn't required for it to be safe to run.

## Logs

Both scripts append to `deploy_webhook.log` / `deploy.log` in the checkout
root (paths overridable via `UL_DEPLOY_LOG_FILE`).

## Production

This is intentionally not wired up for production - production deploys should
stay a deliberate, manual action.