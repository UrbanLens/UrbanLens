# Metrics

The `app` service can expose a Prometheus scrape endpoint at `/metrics`. It is
off by default and unrouted when off.

Scope: HTTP request metrics for the gunicorn web process, Celery queue depth,
and Celery task outcomes from a dedicated exporter service. WebSocket metrics
are deliberately not part of this - see [What is not here](#what-is-not-here).

## Turning it on

```bash
UL_METRICS_ENABLED=true
UL_METRICS_TOKEN=<a long random string>        # and/or
UL_METRICS_ALLOWED_CIDRS=10.2.0.0/24
```

Enabling it on staging or production with neither a token nor an allowlist is a
startup error (`dashboard.E006`). That is not fussiness: the response body is a
map of the application - every view name that has served a request, with its
rate, error rate and latency distribution. Local checkouts are exempt, because
reading it with `curl` while working on it is the point.

Both gates apply when both are set. A request from outside the allowlist gets a
`404`, not a `403`, so a scanner learns nothing; a bad token gets a `401`.

## Reaching it

**nginx returns 404 for `/metrics` on the public vhost.** The endpoint is not
part of the public site, and the vhost should not be the only thing standing
between a misconfiguration and the internet.

So a scraper reaches the app container directly, on the compose network
(`http://urbanlens_app:8000/metrics`) - not through `${UL_APP_PORT}`, which is
nginx's.

### The cross-host problem

Prometheus runs on jungu; UrbanLens runs on chiron. chiron is a VM *on* jungu,
which makes it tempting to assume jungu can just discover these containers. It
cannot, for two independent reasons (verified on chiron, 2026-09-03):

1. **Separate kernel, separate Docker daemon.** `docker_sd_configs` enumerates
   whatever daemon it is pointed at, and jungu's own socket knows only jungu's
   containers. Being a guest VM changes nothing. chiron exposes no Docker API
   either (nothing listening on 2375/2376), so remote `docker_sd` is not
   available - and opening one would hand out root-equivalent access to the
   host, which is a bad trade for a scrape target.
2. **Even if discovery worked, the addresses would be wrong.** `docker_sd`
   yields container bridge IPs - the app sits on `172.30.0.5` - which are
   routable only inside chiron's Docker networks.

Note that plain network reachability between the two hosts is fine, and is not
the problem: jungu already scrapes chiron's cadvisor and node-exporter over
published host ports (33880, 33910). That works because those are
`static_configs`-style targets on *published* ports, not label discovery of
container-internal ones.

**This half is built.** The observability stack's Alloy on chiron now does the
local `docker_sd` that jungu cannot, relabelling on the `prometheus_scrape`
label the `app` service carries, and pushes to jungu via `remote_write`. It is
live and inert: it discovers nothing until a container on chiron actually
advertises the label.

Two things are needed to light it up, and both are on our side:

1. **Deploy a build that has this branch**, with `UL_METRICS_ENABLED=true` and
   `UL_METRICS_TOKEN` set. Until then no container on chiron sets
   `prometheus_scrape=true` and there is nothing to find.
2. **Tell them the app network name** to add to their `UL_SCRAPE_NETWORKS`.
   Alloy lives on `observability-agent_default` and has to join the app's
   network for a discovered container address to resolve. The name is derived
   from the compose project:

   | Deployment | Network |
   |---|---|
   | Shared dev on chiron | `urbanlens-development_main_app_network` |
   | A dev slot on chiron | `ul-<slug>_app_network` |
   | Production / staging | `urbanlens-production_app_network` / `urbanlens-staging_app_network` |

**Production and staging run on damballa, not chiron.** chiron's Alloy discovers
containers on chiron only, so wiring the dev network up does not get production
metrics - that needs an agent on damballa, which is a separate piece of work in
the observability repo.

One deviation from that stack's usual rule, agreed deliberately: this pushes
rather than being scraped. The "a dead agent shows as `up == 0`" property is
moved rather than lost - jungu still scrapes that Alloy as `chiron-alloy`, so
`AgentDown` fires if it dies, and `AlloyComponentUnhealthy` covers Alloy being
up while this pipeline is broken.

## Multiprocess mode, and why it is not optional

Production runs gunicorn with the gevent worker class and `WEB_CONCURRENCY`
processes (3 by default). `prometheus_client`'s default registry is per-process,
so serving it would answer each scrape with whatever the *one* worker that
happened to handle that scrape knows - an endpoint that responds, returns
plausible numbers, and undercounts by roughly the worker count forever.

`PROMETHEUS_MULTIPROC_DIR` is therefore set for the app services, and the
exporter builds a `MultiProcessCollector` over it. Three things keep that
directory honest, in three different files:

| Where | What | Why |
|---|---|---|
| `docker-compose.yml` | Sets the path, on the container filesystem | **Never a shared volume.** Everything in the directory is summed into one scrape, so a directory shared between `app`, `app-ws` and the celery workers would report all of them as whichever was scraped. Every path in the entrypoint's chown loop *is* a shared named volume, which is why this one is handled separately. |
| `docker-entrypoint.sh` | Creates, clears, chowns it | Files are keyed by pid and survive a `docker restart` of the same container; a stale one is summed into every later scrape. |
| `gunicorn.conf.py` | `on_starting` clears it again; `child_exit` calls `mark_process_dead` | `on_starting` runs after `init.py`'s migrate/collectstatic, so the short-lived `manage.py` processes those spawn do not leave files to be counted as a worker's. `child_exit` retires a dead worker's live gauges - see the note below. |

`child_exit` removes only `gauge_live*` files for the exiting pid. Counter and
histogram files deliberately outlive their process (a recycled worker must not
make the service's counters go backwards and break `rate()`), so the directory's
growth is bounded by the clear-at-startup, not by this hook. With the current
metric set - counters and histograms only - `child_exit` removes nothing; it
becomes load-bearing the moment a gauge is added, which Celery in-progress task
counts would do.

A consequence worth knowing: multiprocess mode disables `prometheus_client`'s
default process and platform collectors (`process_cpu_seconds_total`,
`python_gc_*`), because they cannot be meaningfully summed across processes.
cadvisor already provides per-container CPU and memory, so this costs nothing
here.

If `PROMETHEUS_MULTIPROC_DIR` is unset the exporter falls back to the default
registry, which is correct for a genuinely single-process server (`runserver`, a
test) and is the only case where that registry is not a silent undercount.

## What is exported

`django-prometheus`'s `PrometheusBeforeMiddleware` / `PrometheusAfterMiddleware`,
registered outermost and innermost respectively - the difference between the two
timers is what the rest of the middleware stack costs.

Useful series, as they appear in a scrape (Counters carry the `_total` suffix
`prometheus_client` appends, which is what a PromQL query needs):

| Series | What it answers |
|---|---|
| `django_http_requests_latency_seconds_by_view_method` | Latency distribution per view and method |
| `django_http_responses_total_by_status_view_method_total` | Error rate, per view |
| `django_http_requests_total_by_method_total` | Overall request rate |
| `django_http_exceptions_total_by_view_total` | Unhandled exceptions, per view |

Verified against a real 3-worker gunicorn: 31 requests spread across the workers
report as `31` in one scrape, not as one worker's share.

Cardinality is bounded by traffic rather than by the URLconf: labels use
`resolver_match.view_name` (every route here is named, and unmatched paths all
resolve to the `404` catch-all), and a series only exists once a view has
actually served a request. `PROMETHEUS_LATENCY_BUCKETS` is narrowed from the
library default, which spends its resolution below 100ms - finer than anything
here resolves - and its top finite bucket sits above nginx's
`proxy_read_timeout` so a timed-out request is still counted somewhere other
than `+Inf`.

### Celery queue depth

| Series | What it answers |
|---|---|
| `urbanlens_celery_queue_depth{queue=...}` | How far behind each queue is |
| `urbanlens_celery_broker_up` | Whether the broker answered this scrape |

Read from the broker at scrape time rather than instrumented in the workers,
which is what lets it cover the sandbox workers: they run `cap_drop: ALL` on an
isolated network precisely so they have no inbound surface, and an HTTP endpoint
to scrape would undo that. They already talk to the broker, so that is where
their backlog is visible. Labels come from the `Queue` enum, so cardinality is
fixed by code.

`urbanlens_celery_broker_up` exists to keep two states apart that otherwise look
identical and mean opposite things: when the broker cannot be reached the
collector emits **no depth samples at all** rather than zeros, because a row of
zeros reads as a healthy idle system and would silence the alert that should
fire. Alert on `urbanlens_celery_broker_up == 0` as well as on depth.

Each queue is drained by exactly one container, so a rising depth names the
container to look at - see `services/sandbox/queues.py`.

### Celery task outcomes (`celery-metrics` service)

A second endpoint, on its own service, port 8002, **not published to the host**.

| Series | What it answers |
|---|---|
| `urbanlens_celery_tasks_total{task,state}` | Success/failure rate per task |
| `urbanlens_celery_task_runtime_seconds{task}` | How long tasks take |
| `urbanlens_celery_workers_online` | How many workers are heartbeating |
| `urbanlens_celery_events_total{type}` | Whether the event stream itself is flowing |

Its own process, deliberately. A thread in the web tier would run once per
gunicorn worker and triple-count every event; and the Celery workers cannot be
scraped at all, because `media-worker` and `media-worker-batch` run
`cap_drop: ALL` on an isolated network precisely so they have no inbound
surface. Every worker already publishes to the broker, so one consumer covers
all six.

Two details worth knowing:

- **`runtime` is recorded for successes only.** A failed task's duration is the
  time to the exception, which is not comparable to a successful run - folding
  the two together drags the latency percentiles toward whatever the failure
  mode happened to cost.
- **Task names are checked against the local registry**, and anything
  unrecognised collapses to `task="unregistered"`. Names come from workers, not
  from requests, but a rolling deploy can still introduce values, and an
  unbounded label set is how a Prometheus instance falls over.

`CELERY_WORKER_SEND_TASK_EVENTS` follows `UL_METRICS_ENABLED`, so a deployment
not collecting metrics does not pay a broker publish per task transition. Note
`urbanlens_celery_workers_online` and the heartbeat events work regardless of
that flag - only the per-task series need it.

The gate is the same code as the web endpoint
(`services/core/metrics_auth.py`), reached from a bare `http.server` handler
rather than a Django view. One implementation, two transports: a second copy is
how one endpoint ends up open while the other is guarded and nothing says so.
The command refuses to start rather than binding an unguarded port on a
deployed environment.

`PROMETHEUS_EXPORT_MIGRATIONS` is off: it runs a `MigrationExecutor` plan against
the database on every scrape, and `/health/ready` already reports migration state
to the prober that acts on it.

Database and cache instrumentation would need `DATABASES["ENGINE"]` swapped to
`django_prometheus.db.backends.postgis`. Not done, and not a small decision -
it puts a third-party wrapper in the path of every PostGIS query.

## What is not here

- **`app-ws` (Daphne).** Left off deliberately. nginx routes only `/ws/` there,
  so its HTTP request metrics would be an empty set of series; what would
  actually be worth having - active connections, message rate - means
  instrumenting Channels consumers, which is a different piece of work. The
  mechanism is per-service, so turning it on later is one line in
  `docker-compose.yml` plus that instrumentation.
- **k3s.** The equivalent there is a scrape annotation or a `ServiceMonitor`, in
  the `UrbanLens/infrastructure` repo, not this one.
