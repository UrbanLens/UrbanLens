"""Gunicorn configuration for the production ``app`` service.

Loaded explicitly via ``-c gunicorn.conf.py`` in package.json's ``start``
script.

Why ``--worker-connections 20`` is on that command line, since package.json
cannot carry a comment. gevent's default is **1000 greenlets per worker**, and
under ``CONN_MAX_AGE=0`` each greenlet serving a request can hold its own
Postgres backend - so three workers could demand three thousand connections
against ``max_connections=100``. That is the mechanism behind P104's outage,
which read 97 of 100 connections *idle*: 97 is exactly
``max_connections - superuser_reserved_connections``, and idle means a
connection object still alive in some process.

The arithmetic, against 97 usable slots. Everything that is not the web tier
comes to about 32 at full tilt: daphne ~2, celery-worker 4, the panels worker's
20 threads, the two media workers 3, ai-worker 2, beat 1. That leaves ~65 for
the web tier, and 3 x 20 = 60 fits with a little room.

**This bounds the steady state, not the peak, and the difference is worth
knowing.** ``services/core/timeout_utils.py`` holds a module-level
``ThreadPoolExecutor(max_workers=64)`` for external-call deadlines - one per
worker *process* - and its threads can touch the ORM (they call
``close_old_connections`` in a ``finally`` precisely because of that). It is
described there as rarely reached on the request path now that the panels fetch
in Celery, but nothing caps its connection use, so a burst can exceed the
budget above. Per-role ``CONNECTION LIMIT``s are what actually fence that (D11):
a role limit makes the web tier fail its own connections rather than starving
Celery of theirs. This flag removes the unbounded term; it does not make the
number exact.

``--backlog 256`` so overflow queues in the kernel rather than opening a
connection, and D11 replaces both of these with ``gthread``, where in-process
concurrency - and therefore the connection population - is ``workers x threads``
by construction rather than by flag.
"""

import os
from pathlib import Path


def _multiproc_dir():
    """Return the Prometheus multiprocess directory, if this process has one.

    Returns:
        A ``Path`` when ``PROMETHEUS_MULTIPROC_DIR`` is set, else ``None``.
    """
    configured = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    return Path(configured) if configured else None


def on_starting(server):
    """Empty the Prometheus multiprocess directory before any worker forks.

    Multiprocess mode aggregates whatever ``.db`` files it finds, keyed by the
    pid that wrote them. Files from a previous generation of workers are
    therefore still summed into every scrape - counters that no longer have a
    process behind them, and pids that a later worker may reuse. The entrypoint
    already clears this directory at container start; this runs later, after
    ``init.py`` has finished migrate/collectstatic, so the short-lived
    ``manage.py`` processes those steps spawn do not leave their own files
    behind to be counted as a worker's.

    Args:
        server: The gunicorn Arbiter instance.
    """
    directory = _multiproc_dir()
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    removed = 0
    for stale in directory.glob("*.db"):
        try:
            stale.unlink()
            removed += 1
        except OSError:
            server.log.warning("Could not remove stale Prometheus file %s", stale, exc_info=True)
    server.log.info("Prometheus multiprocess dir %s cleared (%d file(s))", directory, removed)


def child_exit(server, worker):
    """Retire a dead worker's live-gauge samples.

    ``mark_process_dead`` removes exactly one thing: this pid's
    ``gauge_live*`` files. Without it, a gauge a worker was maintaining when it
    died - an OOM kill, a reload, a ``max_requests`` recycle - keeps being
    reported at its last value by every later scrape, because nothing else ever
    revisits that file. An in-progress-request gauge stuck above zero forever is
    the shape that takes.

    It does not remove counter
    or histogram files: those must survive their process or a recycled worker
    would make the service's counters go backwards, which breaks ``rate()``. And
    so it does not bound the directory's growth - clearing at startup does that
    (see :func:`on_starting`).

    Today this hook removes nothing, because the request middleware defines only
    counters and histograms and ``PROMETHEUS_EXPORT_MIGRATIONS`` is off. It is
    here for the first gauge anyone adds - Celery in-progress task counts being
    the obvious candidate - at which point it is load-bearing and its absence
    would be a slow, quiet drift rather than a visible failure.

    Gunicorn calls this in the arbiter on child exit

    Args:
        server: The gunicorn Arbiter instance.
        worker: The worker that exited.
    """
    if _multiproc_dir() is None:
        return
    try:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
    except Exception:
        # Never let metrics bookkeeping interfere with reaping a worker.
        server.log.warning("Could not retire Prometheus files for worker %s", worker.pid, exc_info=True)


def post_fork(server, worker):
    """Make psycopg2 cooperative under the gevent worker.

    gunicorn's gevent worker monkey-patches pure-Python socket IO, so
    ``requests`` calls yield to the event loop while waiting on the network --
    but psycopg2 is a C extension that bypasses the patched socket module
    entirely, meaning every database query blocks the worker's whole event
    loop (and with it, every other in-flight request on that worker).
    psycogreen registers psycopg2's wait callback with gevent so DB IO yields
    cooperatively like everything else.

    Only this hook applies the patch, so processes that never load this
    config (celery workers, the daphne app-ws container, manage.py) keep
    stock blocking psycopg2 behaviour, which is correct for them.

    Deliberately does NOT also warm the URLconf (see ``post_worker_init``
    below for why that has to happen later, in a different hook, not just
    later in this same one).

    Args:
        server: The gunicorn Arbiter instance.
        worker: The freshly forked worker process.
    """
    from psycogreen.gevent import patch_psycopg

    patch_psycopg()


def post_worker_init(worker):
    """Warm the URLconf - but only after gevent's own patching has run.

    Args:
        worker: The freshly initialised worker, used for its logger.
    """
    _warm_urlconf(worker)


def _warm_urlconf(worker):
    """Import the URLconf now, so no request has to wait for it.

    Django resolves the URLconf lazily, on a worker's first request, and that
    import reaches every controller (and through them GeoPandas/Shapely).
    Measured on staging: 12.3s for the first request against a fresh process,
    9.5s of it this import. The gevent worker makes that worse than slow - the
    import is CPU that never yields, so the worker serves nothing else for its
    duration, and nginx's proxy_read_timeout is being spent on a page that has
    not started rendering. Doing it here spends it during boot instead, before
    the arbiter routes anything to this process.

    Args:
        worker: The freshly forked worker, used for its logger.
    """
    try:
        import django

        django.setup()
        from django.urls import get_resolver

        # Reading url_patterns is what forces the ROOT_URLCONF import. Logging
        # the count both uses the value (so it cannot be optimised away or read
        # as a mistake) and puts proof in the boot log that this ran.
        patterns = get_resolver().url_patterns
        worker.log.info("URLconf warmed: %d root patterns", len(patterns))
    except Exception:
        # A warm-up is an optimisation. If it fails, the request path will do
        # the same work (and raise the same error) where it can be handled.
        worker.log.exception("URLconf warm-up failed; continuing without it")
