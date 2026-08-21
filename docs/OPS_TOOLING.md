# Ops tooling: staging deploys and ephemeral dev environments

Two tools, both in `bin/`, both stdlib-only so they run on a host where the
project venv does not exist — a deploy tool that needs the thing it is
deploying is useless exactly when it is needed.

- `bin/staging_deploy.py` — deploy staging and **prove it works**.
- `bin/dev_env.py` — create, list and destroy throwaway dev environments.

Both print JSON and exit non-zero when a step failed, so a caller can branch on
the exit code and read the detail only when something breaks.

## Staging: deploy and verify in one call

```bash
python3 bin/staging_deploy.py --branch main --prod-dir /projects/environments/prod/UrbanLens
```

Steps, in this order and for these reasons:

| Step | Why it is where it is |
|---|---|
| `preflight` | Refuses a production checkout, and refuses to `reset --hard` over uncommitted work |
| `pull` / `checkout` | Code first, so the restore runs against the migrations it was dumped for |
| `clone-prod-data` | Reuses `bin/clone_prod_to_staging.sh` |
| `compose-up` | Rebuild and restart |
| `reconnect-nginx` | `compose up` only recreates a service whose own config/image changed — nginx's never does, so a redeployed `app` container leaves nginx pointed at the old one's address, answering 502 to everything below until it is restarted |
| `wait-healthy` | Polls until the site answers — every assertion after this would otherwise race the container it is checking |
| `migrations-applied` | `migrate --check`; migrations run inside the container at start, so this is the first point they can be asserted |
| `data-preserved` | Compares row counts against production's, catching a restore that silently produced a partial database |
| `smoke-pages` | A few URLs, including one that must redirect — a 200 on the landing page alone can be served by a half-booted app |
| `integration-tests` | pytest inside the container, when it's there — staging/production build `--no-dev` for parity, so this step reports "skipped" rather than failing a suite that was never installed |

### Over HTTP, without SSH

`bin/deploy_webhook.py` exposes the same pipeline. Set `UL_OPS_TOKEN` (unset
disables these endpoints entirely) and `UL_PROD_DIR`:

```bash
# Blocks until finished, answers 200 on pass and 500 on fail.
curl -sS -X POST -H "Authorization: Bearer $UL_OPS_TOKEN" \
  "https://staging.urbanlens.org/hooks/staging/deploy?wait=1"

# Or start it and poll.
curl -sS -X POST -H "Authorization: Bearer $UL_OPS_TOKEN" .../staging/deploy
curl -sS -H "Authorization: Bearer $UL_OPS_TOKEN" .../staging/runs/<run_id>
curl -sS -H "Authorization: Bearer $UL_OPS_TOKEN" .../staging/runs/<run_id>/log
```

`?wait=1` is the point: one call, one pass/fail answer, and the log only
matters when the answer is "fail".

Useful flags: `--skip-data` (leave staging's database alone), `--skip-tests`,
`--tests "-k expression"`, `--allow-dirty`.

## Dev: one environment per agent, on demand

```bash
python3 bin/dev_env.py create --owner "agent: floorplans"   # -> https://a7f3c2.dev.urbanlens.org
python3 bin/dev_env.py list                                  # what exists, what is running, which REData
python3 bin/dev_env.py destroy a7f3c2
```

Each environment gets its own checkout of UrbanLens, its own containers, its own
database, and its own hostname. This replaces the three fixed slots
(`s1`/`s2`/`s3`), which ran out and gave no way to tell which were free — `list`
is that answer now.

Ports are allocated in blocks from 31000 and checked against both the registry
and the live socket table: the registry catches an environment that is merely
stopped (it still owns its ports), and the socket check catches anything on the
host that was never registered.

Clones come from the checkouts already on the host when they exist — no
credentials, no rate limit, and git hardlinks the objects, so it costs seconds
and almost no disk.

### REData: shared by default, private by request

REData is **not** duplicated per environment. Three modes, one flag:

| Mode | Flag | What it does |
|---|---|---|
| `production` | *(default)* | Writes the host's `UL_REDATA_API_URL`/`UL_REDATA_API_KEY` into the environment's `.env`. Nothing is built, nothing is started, no port is reserved. |
| `own` | `--own-redata` | Clones REData, starts a private stack, and mints a key in it. |
| `none` | `--no-redata` | Neither. `UL_REDATA_API_URL` is left empty and REData-backed features are inert. |

**Why sharing is the default, and it is not about disk.** REData exposes almost
no write surfaces. Most of its "write" operations do not store anything we send
— they make REData go *fetch* external data about a location. So a throwaway
per-environment REData spends third-party quota pulling data that is then
deleted along with the environment: real API calls, real cost, nothing kept.
Pointed at production, the same calls come back from its cache without
contacting anything external at all, and whatever is genuinely new is cached
there long-term instead of being thrown away. The eight extra containers and
roughly eight minutes on a cold image cache are the *cheap* half of what a
private instance costs.

`--own-redata` is for working **on** the REData integration itself — when the
shared instance's cached answers are the thing under test, or when a schema or
API change has to be exercised before it exists in production.

Credentials are inherited, not invented: the process environment wins (exporting
`UL_REDATA_API_URL`/`UL_REDATA_API_KEY` names which REData an environment should
use), otherwise they come from the primary checkout's `.env`
(`UL_DEV_SEED_ENV`). The key is a real production credential, so it goes into
the new environment's `.env` and nowhere else — never into the run record, the
registry, the JSON the CLI prints, or the log. If neither source has them, the
create records a `warn` saying REData features will be inert and carries on; an
environment without REData is still a working environment.

`--own-redata` still gets its own key minted locally. The inherited one names a
row in a *database*, and a private REData starts with an empty one, so it
authenticates against nothing and every call answers 401 — from a service that
is up, reachable and answering.

The mode is recorded in the registry and shown by `list` as `redata_mode`, so
"is this environment carrying eight idle containers?" is answerable without
docker archaeology. Entries created before modes existed read `unknown`: their
`redata_port` cannot answer it, because one was allocated either way.

### The URL, and why NPM is only touched once

Nginx Proxy Manager runs on **jungu**; the containers run on **chiron**. Rather
than an NPM entry per environment — a two-system operation that leaves orphans
behind when cleanup is forgotten — NPM needs one wildcard entry, once:

```
*.dev.urbanlens.org   ->   chiron:21700
```

Behind it, `bin/opslib/router.py` runs an nginx container **these scripts own**,
matching the `Host` header and forwarding to whichever port that environment
was allocated. Creating or destroying an environment rewrites one file and
reloads; NPM never hears about it.

One-time setup, both per-domain rather than per-environment:

1. A wildcard DNS record `*.dev.urbanlens.org` pointing where the other
   `urbanlens.org` names point.
2. A wildcard certificate for `*.dev.urbanlens.org` on NPM (DNS-01; HTTP-01
   cannot issue wildcards).

An unmatched host gets a 404 from an explicit default server — without it nginx
serves the *first* block to unmatched hosts, so a destroyed environment's URL
would quietly show somebody else's environment.

## Hot reload

```bash
docker compose -f docker-compose.yml -f docker-compose.hot-reload.yml up -d
```

Three things have to line up, and each fails differently alone:

1. **Source is bind-mounted.** Django's autoreloader was already running in
   development — it was just watching the image's frozen copy of `/app/src`,
   which is why the documented workaround has been `docker cp` plus a `chown`.
2. **`.venv` and `node_modules` are shadowed** by anonymous volumes. The host
   `.venv` is built against the host's Python and has no container GDAL, so
   letting the bind mount cover the container's own breaks every GeoDjango
   import at startup.
3. **Logs are redirected** out of the mounted tree via `UL_LOG_DIR`. The host's
   `src/urbanlens/logs` is owned by a different uid than the container's
   `appuser`, and Django's logging config *raises* rather than degrading when it
   cannot open its file — killing the process before it binds a port, with no
   log to say why.

SCSS is watched by a sidecar rather than the app container: a watcher sharing
the container would be killed and restarted by every Python autoreload.

Celery does not autoreload. Its source is mounted for parity, so a restart
picks up edits without a rebuild — but it does need the restart.
