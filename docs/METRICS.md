# Metrics

The `app` service can expose a Prometheus scrape endpoint at `/metrics`. It is
off by default and unrouted when off.

Scope: HTTP request metrics for the gunicorn web process. Celery task metrics
and WebSocket metrics are deliberately not part of this - see
[What is not here](#what-is-not-here).

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

The observability stack's `docker-labeled` scrape job uses **local**
`docker_sd_configs`, so it only discovers containers on the host Prometheus
itself runs on. Prometheus runs on jungu; UrbanLens runs on chiron. That job
cannot discover these containers no matter what labels are set - which the
handoff note that prompted this work caught, and which is why the
`prometheus_scrape` / `prometheus_port` / `prometheus_path` labels on the `app`
service are necessary but not sufficient.

The labels are set anyway (tracking `UL_METRICS_ENABLED`, so a container never
advertises an endpoint it does not serve), because they are what any
*local*-to-chiron discovery would relabel on. The remaining half is a scraper on
chiron:

**Recommended: chiron's Alloy scrapes locally and remote-writes to jungu.**
Alloy already runs there for logs, it is on the same host as the containers, and
it removes the need to publish any new port. Roughly:

```alloy
prometheus.scrape "urbanlens" {
  targets    = [{__address__ = "urbanlens_app:8000", __metrics_path__ = "/metrics"}]
  bearer_token = env("UL_METRICS_TOKEN")
  forward_to = [prometheus.remote_write.jungu.receiver]
}
```

Alloy's container must join `app_network` for that address to resolve.

The alternative - publishing a host port for the app container - exposes the
whole Django app on that port, bypassing nginx, to gain one path. Prefer the
scraper-side fix.

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

`PROMETHEUS_EXPORT_MIGRATIONS` is off: it runs a `MigrationExecutor` plan against
the database on every scrape, and `/health/ready` already reports migration state
to the prober that acts on it.

Database and cache instrumentation would need `DATABASES["ENGINE"]` swapped to
`django_prometheus.db.backends.postgis`. Not done, and not a small decision -
it puts a third-party wrapper in the path of every PostGIS query.

## What is not here

- **Celery task metrics.** A real gap, and independently useful:
  `django-prometheus` has task-signal instrumentation. It needs its own
  multiprocess story (workers are separate processes on separate containers) and
  its own decision about which of the seven queues to instrument, so it is
  separate work rather than something to bundle into "give the web process a
  `/metrics` endpoint" and ship neither cleanly.
- **`app-ws` (Daphne).** Left off deliberately. nginx routes only `/ws/` there,
  so its HTTP request metrics would be an empty set of series; what would
  actually be worth having - active connections, message rate - means
  instrumenting Channels consumers, which is a different piece of work. The
  mechanism is per-service, so turning it on later is one line in
  `docker-compose.yml` plus that instrumentation.
- **k3s.** The equivalent there is a scrape annotation or a `ServiceMonitor`, in
  the `UrbanLens/infrastructure` repo, not this one.
