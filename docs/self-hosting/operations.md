# Operations

## Health endpoints

All are unauthenticated and sit under the app's root. Through the bundled nginx, the site is at `http://127.0.0.1:21800` (or your public host).

| Path | Status | Checks |
| --- | --- | --- |
| `/health/`, `/health/live` | 200, body `Okay!` | Nothing beyond the process answering. The Compose health check uses `/health/`. |
| `/health/ready` | 200 or 503, JSON | Database and cache answer. Also reports `role` (`primary` or `replica`), `migrations` (`current`, `behind` or `unknown`), `connections` (`used` and `max`) and `degraded`. |
| `/health/primary` | 200 or 503, JSON | As `/health/ready`, and the database accepts writes (`role` is `primary`). |

Use `/health/ready` for a load balancer and `/health/primary` before sending writes to an instance. `migrations: behind` is normal during a rolling deploy, and the probes do not fail on it. `degraded` means the instance serves requests but the cache is down or connections are above 80% of the database's limit.

The WebSocket server answers `/health/` on port 8001 inside its container.

## Metrics

Metrics are off by default (`UL_METRICS_ENABLED=false`). When on, the app process serves Prometheus metrics at `/metrics`: one series per view with its request rate, error rate and latency. That is a map of the application, so the endpoint is guarded:

- `UL_METRICS_TOKEN`: the scraper sends `Authorization: Bearer <token>`.
- `UL_METRICS_ALLOWED_CIDRS`: networks the scraper may connect from. The client address is read through `UL_TRUSTED_PROXY_COUNT`, as for rate limits.
- When both are set, both must pass. In production, enabling metrics with neither set stops startup (`dashboard.E006`).

The `/metrics` route is not published. nginx returns 404 for it, and Compose publishes no port for the app. Scrape it from another container on the Compose network, for example `http://urbanlens_app:8000/metrics`.

Celery's exporter is a separate service, `celery-metrics`, on port 8002. It is in the `metrics` Compose profile, so it runs only when you enable that profile. Set `COMPOSE_PROFILES=metrics` together with `UL_METRICS_ENABLED=true`, or the exporter stays absent without any error.

## Logs

- `docker compose logs -f <service>` shows each service's console output. The application services keep up to 10 log files of 200 KB each.
- `daphne-access.log`, `init.log` and the beat schedule file live in the `logs` volume at `/var/log/urbanlens`.
- Django's own file log (`django.log`) is off in the containers: the image sets `UL_LOG_DIR` blank, so Django logs to the console only. Set `UL_LOG_DIR=/var/log/urbanlens` in `.env` to also write `django.log` (rotated, 10 MB by 5 files) into the `logs` volume, which survives recreation. Every service writes the same file, so a rotation can drop a few lines; the console output stays complete. Outside Compose, an unset `UL_LOG_DIR` writes `logs/` under the project root.
- Logs redact API keys from error text, and the health probes are left out of the access log.

## Services and Celery queues

| Service | Queue | Runs |
| --- | --- | --- |
| `celery-worker` | `interactive` | Work a person is waiting on, and safety deadlines. |
| `celery-worker-bulk` | `bulk`, `maintenance`, `celery` | One account's large jobs, site-wide scheduled maintenance, and the default queue. |
| `celery-worker-panels` | `panel_fetch` | External-data fetches for the private pin page. |
| `media-worker` | `sandbox` | Interactive parsing of uploaded bytes (images, video, documents). |
| `media-worker-batch` | `sandbox_batch` | Long parses of uploaded bytes, such as archive walks and imports. |
| `celery-beat` | none | Schedules tasks onto the queues above. |

The two media workers run in `sandbox_network`, which has no route out, with all capabilities dropped and a process limit. Set `UL_SANDBOX_ENABLED=false` only where no media worker runs, or uploads will queue with nothing to process them.

`celery-beat` gets its environment from `docker-compose.yml` and does not load `.env`, so it holds no API keys. Compose passes it the settings its schedule is computed from: `UL_ENVIRONMENT`, `UL_BACKGROUND_TASKS_ALLOWLIST`, `UL_ENVIRONMENT_SHARE`, `UL_ENVIRONMENT_SHARE_OVERRIDES`, `UL_DISABLED_PLUGINS` and `UL_PLUGIN_MODULES`. A plugin that reads its own settings to decide whether to schedule anything needs those added to the `x-beat-env` list in `docker-compose.yml`.

Each worker runs with a soft and a hard time limit per task, and recycles its child processes after a number of tasks or memory use (`UL_CELERY_*`). See `.env-sample`.

## ClamAV

The `clamav` service runs `clamd`. Every upload is scanned before it is stored, when `UL_CLAMAV_ENABLED` is on (the default). The app connects to `UL_CLAMAV_HOST:UL_CLAMAV_PORT` (`urbanlens_clamav:3310`). While the scanner is enabled but unreachable, uploads are refused. The service has no health check in Compose, so check it directly if uploads fail after a start:

```bash
docker compose logs clamav
```

## Scheduled tasks

Scheduled tasks are listed in `CELERY_BEAT_SCHEDULE` in `src/urbanlens/UrbanLens/settings/base.py`. They cover housekeeping and retention pruning, the hourly backup check, safety check-in reminders and escalation, account deletion, and sweeps for work that stalled. Entries that reach outside the deployment run only in production, unless named in `UL_BACKGROUND_TASKS_ALLOWLIST`. See [configuration](configuration.md#outbound-calls-and-the-egress-policy).

## External API

The external API for third-party clients is under `/dashboard/api/external/v1/`.

- Schema (OpenAPI): `/dashboard/api/external/v1/schema/`
- Interactive docs (Swagger UI): `/dashboard/api/external/v1/docs/`

Both are public. They describe only the external surface.

Requests authenticate with `Authorization: Bearer <credential>`. A credential is either:

- an **API key**, created from the security settings of an account. Each key carries scopes and can be revoked there; or
- an **OAuth2 access token** from the django-oauth-toolkit provider under `/oauth/`. Its authorization endpoint is `/oauth/authorize/`, and its token introspection endpoint is `/oauth/introspect/`. Default scopes are `profile:read`, `pins:read`, `pins:write` and `push:manage`.

A credential can only do what its scopes allow, whatever the account can do otherwise.

Rate limits are per credential, in DRF's `N/period` form: `UL_EXTERNAL_API_READ_RATE` (default `1000/hour`), `UL_EXTERNAL_API_WRITE_RATE` (`300/hour`) and `UL_EXTERNAL_API_BURST_RATE` (`60/minute`).

For the first-party native client, `manage.py provision_mobile_oauth_client` creates or updates its public OAuth2 registration. It is idempotent. Run it only if you run that client.
