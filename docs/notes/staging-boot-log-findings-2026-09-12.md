# N22 — Staging could not start, and its logs held five more defects

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N22` · `status: current` · `updated: 2026-09-12`

Staging was deployed from `release/v_0_8_0` and crash-looped. The cause took minutes; the
interesting part is that the whole class of defect below is invisible to the test suite, and
four of the six were only findable by reading the logs of a process that had actually started.

## H59 — nginx refused the config, and three tests said it was fine

`limit_conn_zone` was added to `nginx.conf` inside `events {}`. It is an `http`-level directive,
so nginx exited with `"limit_conn_zone" directive is not allowed here` and never bound a port.
Not degraded — **gone**, every request, before anything reached Django.

Three tests covered this exact directive (`test_socket_budget.py`, the H10 socket-cap work) and
all three passed. Each was an `assertIn` against whole-file text, which cannot see which block a
directive landed in. One of them said "and in the right place" in its own docstring.

Two things made it survive:

* **The config is bind-mounted.** A running nginx serves from what it parsed at boot, so the
  break is invisible until something restarts. The local dev container was reporting `healthy`
  while `nginx -t` inside it failed — it had been serving from an unreloadable config for 19
  hours. Production was untouched only because its checkout predates the commit.
* **pre-commit could not have caught it.** The top-level `files:` filter in
  `.pre-commit-config.yaml` admits `py|ts|js|tsx|jsx|json|sh|yaml|yml|toml|html|txt` — no `.conf`.
  A path-scoped hook on these files would never fire, which is why the new one is `always_run`.

Fixed by moving the directive, plus `core/tests/nginx_config.py` (parses the config into
directives-with-contexts, so placement is checkable) and `bin/check_nginx_config.sh`, which runs
the real `nginx -t` in the same image compose uses. The script was verified by breaking the config
and watching it reproduce the outage message — a config checker that has never failed is a config
checker nobody has tested.

## H60 — the AI service was running the app's gunicorn config

gunicorn reads `gunicorn.conf.py` from the working directory when nothing names it with `-c`, and
that directory is `/app` for every service built from this image. So the config written for the
Django app also configured `ai-inference`, which runs `urbanlens_ai.wsgi` — no Django, no ORM, no
`DJANGO_SETTINGS_MODULE`, that isolation being the entire point of the service.

The file's own docstring said "Loaded explicitly via `-c` in package.json's `start` script", which
is how this stayed invisible.

* `post_fork` applied psycogreen's **gevent** patch inside a **gthread** worker. Inert today only
  because that service happens never to open a database connection — a property of today's AI app,
  not of the hook. The same hook is what D11 has to get right when the app itself moves to gthread.
* `post_worker_init` called `django.setup()` and logged an `ImproperlyConfigured` traceback on
  every boot, for a service configured exactly as intended.

Fixed: each hook now checks the process it landed in. `--no-control-socket` was also added to that
service, which is `read_only` and so could not create gunicorn's control socket under `$HOME`.

## H61 — the egress proxy's healthcheck was flooding its own log

`test: ["CMD", "nc", "-z", "127.0.0.1", "8888"]` opens a TCP connection and closes it without
sending anything, so tinyproxy logged `ERROR ... read_request_line: Client closed socket before
read.` every 30 seconds — 2,880 error lines a day against a json-file driver holding 200 KB × 10.
Real errors from this container aged out within days.

This is the only network boundary the AI tier has (`docs/AI_PIPELINE.md`), so it is the log least
able to afford noise: a blocked or failing provider call surfaces here and nowhere else.

Replaced with a complete request line. The `403 Filtered` answer proves tinyproxy accepted the
connection, parsed the request, consulted the allowlist and wrote a response; `nc -z` proves only
that a port is open, which a wedged process also manages. The probe deliberately names a host the
allowlist refuses, so it exercises the boundary without sending real egress — and a test asserts
nobody later makes it return 200 by adding that host to `config/egress/filter`, which would put a
hole in a security boundary to satisfy a probe.

Verified behaviourally: ERROR count over 100 seconds went from 3 to 0.

## H62 — daphne stalled the socket tier importing the URLconf

Staging's first `/health/` on `app-ws` cost **4184 ms wall against 4176 ms CPU and zero SQL**, on
every restart.

`asgi.py` imports the *websocket* patterns only, so the HTTP URLconf — which reaches every
controller and through them GeoPandas/Shapely — stayed unloaded until the first HTTP route was
asked for. Measured in-container: **2.26 s CPU** for that import, on top of **2.19 s** for
`django.setup()`.

`app-ws` runs with `cpus: 1` and that import holds the GIL, so the cost lands on the event loop
serving every WebSocket on the site — *after* the container has reported itself healthy. gunicorn
has solved this since P104 in `post_worker_init`; daphne loads no gunicorn config, so the one
process that most needed a warm start was the one without it.

Fixed in `asgi.py`. The test runs its probe in a fresh interpreter: an in-process check passes
against the unfixed module, because the suite imports the URLconf long before any test runs.
Verified on staging — `URLconf warmed: 30 root patterns` now appears at boot and the slow-request
line is gone.

## H63 — the landing page costs ~1.4 s of CPU once per worker (open)

`slow request view=index wall_ms=1458 cpu_ms=1415 sql_ms=17 sql_n=3` — three times after a
restart, for three gunicorn workers, and never again. Reproduced in-container at **0.568 s CPU**
for the first `GET /` against **0.005 s** for every later one, a ~100× cold cost.

It is **not** the URLconf: the app already warms that, and the measurement above was taken with it
warm. `cProfile` puts the cost inside `TemplateResponse.rendered_content` — the render itself,
not the lookup.

**The obvious fix does not work, which is the useful part of this entry.** Warming the template
with `get_template('dashboard/pages/home/index.html')` costs 0.018 s and moves the first request
only from 0.568 s to 0.528 s. So it is not compilation of the top-level template; it is the
lazily-loaded `{% include %}` chain and the tag-library imports those trigger during render.
Warming it properly means rendering the page once at boot, which needs a request object.

Left open deliberately. Aggregate waste is small — one worker recycle per `--max-requests 1000` —
but under gevent that 1.4 s is CPU that never yields, so up to 19 other in-flight requests on that
worker wait for it. That is the standing requirement's own failure shape, at low severity.

## H64 — a permanently unreadable file is retried hourly, forever (open)

`generate_image_thumbnails` (`tasks.py:1432`) logged 50 tracebacks on staging for files the
database references and the disk does not have (2,444 `Image` rows against 25 files — a restored
database with an unrestored media volume, environmental rather than a bug).
`generate_image_marker_thumbnails` (`tasks.py:1675`) does the same over the same rows, so each
unreadable file is reported twice per wrap; a later 10-minute window showed 50 more from the
marker path alone, which is how the loop was confirmed to be ongoing rather than a boot artifact.

The design around both is sound: the backfill walks by primary key, the cursor advances past
failures, and an exhausted cursor resets. But that means a file that can *never* be read is
retried on every wrap, logging a full traceback each time, with no way to stop.

The codebase already has the concept — `write_image_preview` caches `UNPREVIEWABLE` against a
failure TTL so a bad file is not re-examined. The thumbnail path has no equivalent. Low severity
(the batch is bounded and it is off the request path), recorded because the fix is a known shape
already used a few lines away.

## Not defects

* **`celery-metrics` exited (137) two days ago** — N15's fix has landed; the service now carries
  `profiles: ["metrics"]`, so `docker compose up` correctly does not start it. The container is a
  leftover from before that gate.
* **`DisallowedHost: 127.0.0.1`** in the app log — caused by this session's own `wget` probes
  without a `Host` header, not by the container healthcheck.
* **`Could not decrypt Image.exif_data`** (7×) — staging's field-encryption key does not match the
  data restored into it. Environmental; the rows are left intact and recoverable by design.
* **`connect() failed (111: Connection refused)`** in nginx — requests arriving during the app's
  own restart window. A single-upstream deploy has no instance to fail over to; that is a
  deployment-strategy question, not a code defect.
