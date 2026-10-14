# Install

This takes a checkout from zero to a running site with Docker Compose. Read it through before the first `up`: a production deployment refuses to start with missing or shared secrets.

For a single machine without TLS, skip to [Local trial](#6-local-trial-over-plain-http).

## 1. Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose version`).
- Git.
- A public hostname and a TLS-terminating reverse proxy you run (see [section 5](#5-tls-and-the-reverse-proxy)).
- An SMTP account for sign-up and password-reset email (see [section 8](#8-email)).
- Python 3 on the host, to generate secrets. Any source of random strings works.

## 2. Get the code and create `.env`

```bash
git clone https://github.com/UrbanLens/UrbanLens.git urbanlens
cd urbanlens
cp .env-sample .env
```

Compose reads `.env` from the project directory for variable substitution and passes it to the services that load it. `.env` is excluded from the image build.

## 3. Set the required values

Edit `.env`. Every variable is commented in `.env-sample`; these are the ones a public deployment must set.

| Variable | Requirement |
| --- | --- |
| `UL_ENVIRONMENT` | `production` for a public site. Unset, blank or an unknown name refuses to start. Only `production` (and `staging`, `local` and `testing`) run gunicorn; `development` starts Django's `runserver`, which is not for a public site. |
| `DJANGO_SECRET_KEY` | Required outside `local`, `development` and `testing`. Signs sessions, CSRF tokens and password-reset links. |
| `UL_FIELD_ENCRYPTION_KEY` | Encrypts sensitive columns (OAuth tokens, TOTP seeds, contact details, private notes). Not checked at startup: when unset it is derived from `DJANGO_SECRET_KEY`. Set it separately, and never change it in place (see [backups](backups.md)). |
| `UL_DB_PASS` | Password of the database owner, used by the `db` container and the migration step. |
| `UL_DB_APP_PASS` | Password of the per-service database logins. Required in production, and must differ from `UL_DB_PASS`. |
| `UL_RABBITMQ_PASS` | Password of the bundled RabbitMQ broker. Compose defaults it to RabbitMQ's published `guest`, which a deployment is warned about at startup (`dashboard.W005`). `UL_RABBITMQ_USER` and `UL_RABBITMQ_VHOST` default to `urbanlens`. The broker only applies these when its volume is first created; to change the password later, change it in RabbitMQ (`rabbitmqctl change_password`) and in `.env`. |
| `UL_SITE_URL` | Required outside `local`, `development` and `testing`, and must not be a loopback address. The public URL, for example `https://your-domain.example.com`. |
| `UL_ALLOWED_HOSTS` | Comma-separated hostnames the site answers to. Keep `localhost` and `127.0.0.1` in the list: the container health checks use them. Add the public hostname. |

Generate each secret separately:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # DJANGO_SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # UL_FIELD_ENCRYPTION_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # UL_DB_PASS
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # UL_DB_APP_PASS
```

Database passwords may contain only letters, digits and `!@#$^*()_-`, which `token_urlsafe` output satisfies. Set the passwords before the first start. The PostgreSQL image applies `POSTGRES_PASSWORD` only when it creates the data volume, so changing `UL_DB_PASS` later also means changing it inside PostgreSQL.

`UL_FIELD_ENCRYPTION_KEY` must be at least 32 characters with at least 16 distinct ones; `token_urlsafe(64)` output meets that.

## 4. Build and start

```bash
docker compose up -d --build
```

Startup order:

1. `db` starts (PostgreSQL 17 with PostGIS).
2. `db-setup` runs once: it creates the database if needed, enables PostGIS, runs `migrate`, and creates or updates the per-service database logins. It exits when done, and the other services wait for it.
3. `app`, `app-ws`, the workers, `celery-beat`, `nginx` and the rest start.

Check it:

```bash
docker compose ps
docker compose ps -a db-setup                 # State should be exited with code 0
docker compose logs db-setup
curl -fsS http://127.0.0.1:21800/health/ready      # or your UL_APP_PORT
```

The readiness probe returns JSON. A healthy production instance reports `"db":"ok"`, `"cache":"ok"`, `"role":"primary"` and `"migrations":"current"`. See [operations](operations.md#health-endpoints) for the other probes. The `app` container's health check has a 300-second start period.

Only two ports are published on the host: `21800` (the site, through `nginx`, set by `UL_APP_PORT`) and `21801` (media, through `media-nginx`, set by `UL_MEDIA_PORT`). PostgreSQL, Dragonfly, RabbitMQ, ClamAV and the application servers are reachable only inside the Compose networks.

## 5. TLS and the reverse proxy

Terminate TLS in a proxy you run, and forward requests to `127.0.0.1:21800`. The bundled nginx expects the following.

- **`X-Forwarded-Proto: https`.** Set it on every request. nginx passes the header through, and when it is missing nginx sends `http`, so Django redirects each request to HTTPS and the browser loops.
- **`Host`** passed through unchanged. The public name must be in `UL_ALLOWED_HOSTS`.
- **`X-Forwarded-For`** appended to, as proxies do by default. Keep `UL_TRUSTED_PROXY_COUNT` at its default of `1`. The bundled nginx appends one entry, and rate limits read the client address from there. Setting it to `0` behind the bundled nginx makes every request look like it came from nginx.
- **WebSocket upgrades** on `/ws/`. nginx sets `Upgrade` and `Connection` for that location, so the proxy must not drop them.
- **Source address.** The bundled nginx takes the client address from `X-Forwarded-For` only when the connection comes from a trusted range, by default `172.16.0.0/12` and `192.168.0.0/16`. A proxy on the same host connecting to `127.0.0.1:21800`, or a proxy container on the Compose network, arrives from those. A proxy on another machine, or anything else outside those ranges, does not: nginx records the proxy as the client, and every visitor shares one rate-limit key. Set `UL_TRUSTED_PROXY_CIDRS` in `.env` to the proxy's address or network, space- or comma-separated (for example `UL_TRUSTED_PROXY_CIDRS=10.0.0.5`), then recreate `nginx` and `media-nginx`. The list replaces the default, so include a default range if the proxy also connects from one. List only the proxy: a range that visitors can send from lets them choose the address they appear to have.

In production, Django redirects plain-HTTP requests to HTTPS, except the `/health` paths. Session and CSRF cookies are marked `Secure`. HSTS is sent with `UL_HSTS_SECONDS` (default `31536000`), and `UL_HSTS_INCLUDE_SUBDOMAINS` defaults to on whenever HSTS is. If other subdomains of your domain serve plain HTTP, set `UL_HSTS_INCLUDE_SUBDOMAINS=false`.

Uploads: nginx accepts requests up to 200 MB. A proxy in front of it with a lower limit rejects larger requests itself, with its own error page. Set `UL_MAX_REQUEST_BODY_MB` to the proxy's limit, so the site refuses oversized uploads with its own message.

Media on a separate host is optional. With `UL_MEDIA_BASE_URL` unset, media is served from the site's own host. To use a separate host, first point it at `127.0.0.1:21801` and confirm it resolves, then set `UL_MEDIA_BASE_URL`; the sample comments give the order. See [configuration](configuration.md#media-storage).

## 6. Local trial over plain HTTP

To run the stack on one machine without TLS, change the environment in `.env`:

```bash
UL_ENVIRONMENT=local
DJANGO_SECRET_KEY=<any generated value>
UL_SITE_URL=
```

- `local` allows plain HTTP (`UL_UNSAFE_ALLOW_HTTP` defaults to on there), and it does not require `UL_SITE_URL`: blank, as above (the sample's `https://your-domain.example.com` would send links to a site that isn't there), it falls back to `http://localhost:21800`, or to `UL_APP_PORT` if you changed it.
- `local` and `development` default `DJANGO_DEBUG` to on. Set `DJANGO_DEBUG=false` if anyone else can reach the trial.
- Use `local`, not `development`, for a trial that runs like production. In `development` the container starts Django's `runserver` instead of gunicorn.
- Set `DJANGO_SECRET_KEY` even in a trial. Without it each gunicorn worker generates its own key, and logins do not survive a request that lands on another worker.
- The production password checks do not apply in `local`, so the sample passwords work. Replace them anyway.
- Mail is printed to the log in `local`, verification links included. Read them with `docker compose logs app`.

Then run `docker compose up -d --build` and open `http://localhost:21800`. To reach the trial from another machine, add its hostname or address to `UL_ALLOWED_HOSTS` and set `UL_SITE_URL=http://<address>:21800`.

## 7. First admin

The first account created on an empty site becomes the bootstrap site administrator. This is decided when the account row is created (`dashboard/services/admin/site_admin.py`, `promote_first_user_if_needed`):

- With `UL_BOOTSTRAP_ADMIN_EMAIL` unset, the first account is promoted.
- With it set, the first account is promoted only if its email matches (case-insensitive, with Gmail normalization). If the first account has another email, nobody is promoted, and later sign-ups are not promoted either. Use the `createsuperuser` route below in that case.

To create the admin through the site:

1. Open `/signup/` and register with the admin's email address.
2. In production the account is inactive until the verification link sent to that address is opened, so [email](#8-email) must work first.
3. Log in. The bootstrap administrator is redirected to the setup wizard at `/dashboard/setup/`. The wizard asks for the site title and lists which integrations are configured, by environment variable name. Completing it marks onboarding done.

To create a Django superuser instead, which needs no email verification:

```bash
docker compose exec -u appuser app python src/urbanlens/manage.py createsuperuser
```

Site administrators can turn public sign-up off, so that only invited people can join, from the site admin settings (`signup_restricted`).

## 8. Email

Production sends mail over SMTP. Set these in `.env`:

- `UL_EMAIL_HOST`, `UL_EMAIL_PORT` (default `587`), `UL_EMAIL_USER`, `UL_EMAIL_PASSWORD`
- `UL_EMAIL_TLS` (default `true`, STARTTLS) or `UL_EMAIL_USE_SSL` (default `false`)
- `UL_EMAIL_FROM` (the code default is `noreply@yourdomain.org`, so set it)

Sign-up verification, password reset and safety and notification mail all depend on it. Outside production (`local`, `development`, `staging`) mail is printed to the log instead, unless `UL_EMAIL_SEND_OUTSIDE_PRODUCTION=true`. Mail to reserved domains such as `example.com` is refused before it reaches the backend.
