# Configuration

`.env-sample` is the complete reference: every setting it names is commented there with its default and its effect. This page explains how settings are read and what the groups are for.

## How settings are read

- Settings come from the process environment. For a Compose deployment, that is `.env` in the project directory plus the `environment:` lists in `docker-compose.yml`.
- Most application settings are `UL_`-prefixed and are read by pydantic-settings in `src/urbanlens/UrbanLens/settings/app.py`. A field such as `email_host` is set by `UL_EMAIL_HOST`.
- A few are read directly in `src/urbanlens/UrbanLens/settings/base.py` without the prefix: `DJANGO_SECRET_KEY` and `DJANGO_DEBUG`, plus `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE`. `DJANGO_DEBUG` is the only control for Django's debug mode; a plain `DEBUG` is ignored. Likewise there is no `SECURE_SSL_REDIRECT` variable: the HTTPS redirect follows `UL_UNSAFE_ALLOW_HTTP`.
- A blank value counts as unset and takes the default.
- Booleans accept `1 true t yes y on` and `0 false f no n off`. Anything else refuses to start.
- An invalid value, or a missing required value, makes the process refuse to start.
- `.env` is not copied into the image. After editing it, run `docker compose up -d` to recreate the containers.

## Groups

**Environment and identity.** `UL_ENVIRONMENT` (`local`, `development`, `testing`, `staging`, `production`), `UL_SITE_URL`, `UL_ALLOWED_HOSTS`, `UL_CONTAINER_NAME` (names the containers, so two stacks can share a host), `UL_APP_PORT`, `UL_MEDIA_PORT`.

**Secrets.** `DJANGO_SECRET_KEY`, `UL_FIELD_ENCRYPTION_KEY`, `UL_FIELD_ENCRYPTION_KEY_FALLBACKS` (retired encryption keys, comma-separated; see [backups](backups.md#rotating-the-field-encryption-key)).

**Database.** `UL_DB_NAME`, `UL_DB_PASS`, `UL_DB_APP_PASS`, `UL_DB_ROLES_POOLED`, and the connection tuning variables (`UL_DB_CONN_*`). Compose sets the host and user for the bundled database.

**Cache, broker and channels.** `UL_DRAGONFLY_URL`, `UL_PROXY_CACHE_URL`, `UL_CHANNEL_LAYER_URL`, `UL_RABBITMQ_URL`, or the parts Compose builds it from: `UL_RABBITMQ_USER`, `UL_RABBITMQ_PASS`, `UL_RABBITMQ_VHOST`. Compose's defaults are user `urbanlens` and password `guest`, so set `UL_RABBITMQ_PASS`. The broker is not published on the host.

**Email.** `UL_EMAIL_*`, `UL_EMAIL_SEND_OUTSIDE_PRODUCTION`, `UL_ADMIN_NOTIFICATION_EMAIL` (receives critical site notifications; also editable in site admin). See [install](install.md#8-email).

**Proxy and HTTP.** `UL_TRUSTED_PROXY_COUNT`, `UL_UNSAFE_ALLOW_HTTP`, `UL_HSTS_SECONDS`, `UL_HSTS_INCLUDE_SUBDOMAINS`, `UL_MAX_REQUEST_BODY_MB`, `UL_CSP_ENFORCE`. See [install](install.md#5-tls-and-the-reverse-proxy).

**Uploads and scanning.** `UL_CLAMAV_ENABLED` (default on), `UL_CLAMAV_HOST`, `UL_CLAMAV_PORT`, `UL_CLAMAV_TIMEOUT_SECONDS`, `UL_SANDBOX_ENABLED`, `UL_UNTRUSTED_PARSE_POLICY`. Uploads are refused while the scanner is enabled but unreachable. Turn ClamAV off only where the `clamav` service does not run.

**Celery.** `UL_CELERY_TIMEZONE`, task time limits (`UL_CELERY_TASK_*_TIME_LIMIT`), worker recycling (`UL_CELERY_WORKER_MAX_*`). Setting `UL_CELERY_TASK_ALWAYS_EAGER` runs tasks in the request; that is for tests and debugging only.

**Metrics.** See [operations](operations.md#metrics).

**Limits.** Upload sizes, rate limits (including `UL_EXTERNAL_API_*`), WebSocket limits, and search and map ceilings. The defaults suit a single web host. Raise one only with measurements behind it.

**Backups.** See [backups](backups.md#settings).

**Plugins and pages.** `UL_PLUGIN_MODULES`, `UL_DISABLED_PLUGINS`, `UL_TEMPLATE_OVERRIDES_DIR`, `UL_VENDOR_ASSET_BASE_URL`. See below.

## Outbound calls and the egress policy

Each external service is classified in `src/urbanlens/UrbanLens/egress.py`, and the environment decides what may be called:

| Category | production | staging | development, local |
| --- | --- | --- | --- |
| first-party and internal services | allowed | allowed | allowed |
| deployed services | allowed | allowed | refused |
| quota-limited and billed services | share of budget | share of budget | share of budget (0 by default) |
| messaging (mail, SMS, WhatsApp) and third-party writes | allowed | refused | refused |

- `UL_ENVIRONMENT_SHARE` is the fraction (0 to 1) of every quota-limited and billed budget this deployment may use. Unset, it is 0.95 in production, 0.05 in staging, and 0 elsewhere. Each service declares its call limits in code, and the share scales them.
- `UL_ENVIRONMENT_SHARE_OVERRIDES` sets a share per service, for example `nominatim=0.02,google_geocoding=0.01`. For messaging and third-party writes, any value above 0 turns the service on outside production, and 0 turns it off in production.
- `UL_BACKGROUND_TASKS_ALLOWLIST` names scheduled tasks that reach outside the deployment and should run outside production.

A self-hosted production deployment gets 95% of each quota-limited budget by default. If a provider stops answering, check the share before anything else.

## API keys for optional providers

Each key is optional. Without it, the feature it names is unavailable or falls back to a keyless source.

| Variables | Enables |
| --- | --- |
| `UL_GOOGLE_CLIENT_ID`, `UL_GOOGLE_CLIENT_SECRET` | Sign-in with Google, and the Google Calendar and Google Photos integrations. |
| `UL_DISCORD_CLIENT_ID`, `UL_DISCORD_CLIENT_SECRET` | Sign-in with Discord. |
| `UL_GOOGLE_UNRESTRICTED_API_KEY` | Google Maps and Places lookups, through the built-in Google plugins. |
| `UL_GOOGLE_PUBLIC_API_KEY` | Street View replay on the private pin page. The key is sent to the browser. |
| `UL_OPENWEATHERMAP_API_KEY` | Weather for locations. |
| `UL_APPLE_MAPS_API_KEY` | Apple Maps lookups (a pre-generated JWT). |
| `UL_AZURE_MAPS_SUBSCRIPTION_KEY` | Azure Maps lookups. |
| `UL_USGS_API_KEY`, `UL_USGS_USERNAME` | USGS EarthExplorer and The National Map data. Both are needed. |
| `UL_FLICKR_API_KEY`, `UL_FLICKR_API_SECRET` | Importing photos from Flickr. |
| `UL_VIRUSTOTAL_API_KEY` | Hash lookups of fetched images. |
| `UL_PROTOMAPS_API_KEY` | Protomaps basemap tiles. Unset, tiles come from this deployment's own mirror. |
| `UL_TWILIO_ACCOUNT_SID`, `UL_TWILIO_AUTH_TOKEN`, `UL_TWILIO_SMS_FROM_NUMBER`, `UL_TWILIO_WHATSAPP_FROM_NUMBER` | SMS and WhatsApp notifications. |
| `UL_OSRM_BASE_URL` (no key) | Drive-time routing. Blank uses OSRM's public demo server, which its project documents as for development only. |
| `UL_OVERPASS_API_URL` (no key) | Your own Overpass endpoint. Blank asks the public mirrors, where the environment allows them (production by default). |

## Media storage

By default uploads are stored on the `media_volume` Docker volume, mounted at `src/urbanlens/media` in the containers.

To store them in an S3-compatible bucket (Garage, MinIO, AWS), set `UL_MEDIA_STORAGE_BACKEND=s3` with `UL_S3_ENDPOINT_URL`, `UL_S3_BUCKET_NAME`, `UL_S3_ACCESS_KEY_ID`, `UL_S3_SECRET_ACCESS_KEY` and `UL_S3_REGION_NAME`. `UL_S3_ADDRESSING_STYLE=path` suits most self-hosted stores. The system check refuses to start without the bucket name and the access keys. Files stay behind the media gate: the site serves `/media/...` URLs, never a presigned bucket URL.

With the S3 backend, the media volume still holds some local-only directories (`exports/`, `imports/`, `preview_sources/`, `import_previews/`, `confirmed_imports/`). The system check reports them as a warning. Back up the volume as well as the bucket. See [backups](backups.md#what-to-back-up).

## Media origin

Uploads can be served from their own host (`UL_MEDIA_BASE_URL`), so that anything that slips past validation runs in an origin with no session cookie. The `media-nginx` service listens on `UL_MEDIA_PORT`. The media host must also be in `UL_ALLOWED_HOSTS`. Order matters: serve the hostname first, then set `UL_MEDIA_BASE_URL`, which rewrites every media URL at once.

`UL_MEDIA_COOKIE_DOMAIN` is derived from the two hostnames when blank. Set it only when they are not sibling subdomains. `UL_MEDIA_CSP` replaces the default media content policy, which is `default-src 'none'`.

## Plugins

Plugins add features and pages. `UL_PLUGIN_MODULES` loads further plugin modules (dotted paths, comma-separated), and `UL_DISABLED_PLUGINS` turns off discovered plugins by name. `manage.py plugins` lists what is loaded. Writing and packaging plugins is covered in [docs/plugins/](../plugins/README.md).

## Replacing pages

`UL_TEMPLATE_OVERRIDES_DIR` names a directory of templates that replace bundled templates. A file there replaces the bundled template with the same relative path. For example, to replace the privacy policy, create:

```
<UL_TEMPLATE_OVERRIDES_DIR>/dashboard/pages/legal/privacy.html
```

Other bundled pages you may want to replace are `dashboard/pages/legal/terms.html`, `dashboard/pages/faq/index.html` and `dashboard/pages/about/index.html`. A replacement can `{% extends %}` the bundled template of the same name and override only the blocks it changes.

The override directory is searched before plugin and bundled templates. The directory must be visible to the containers: mount it into `app` and the workers, or bake it into a derived image.

Third-party JavaScript and CSS (Leaflet, htmx, Chart.js and the rest) is loaded from public CDNs by default. `UL_VENDOR_ASSET_BASE_URL` points them at a mirror that keeps the same paths. The list of paths is `VENDOR_ASSETS` in `src/urbanlens/dashboard/services/core/vendor_assets.py`.
