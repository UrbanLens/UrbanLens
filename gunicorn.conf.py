"""Gunicorn configuration for the production ``app`` service.

Loaded explicitly via ``-c gunicorn.conf.py`` in package.json's ``start``
script. Worker count is not set here: gunicorn reads the ``WEB_CONCURRENCY``
environment variable natively (see docker-compose.yml, where it defaults
to 3).
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

    Two things it deliberately does not do, worth knowing before reading a
    directory listing and concluding this is broken. It does not remove counter
    or histogram files: those must survive their process or a recycled worker
    would make the service's counters go backwards, which breaks ``rate()``. And
    so it does not bound the directory's growth - clearing at startup does that
    (see :func:`on_starting`).

    Today this hook removes nothing, because the request middleware defines only
    counters and histograms and ``PROMETHEUS_EXPORT_MIGRATIONS`` is off. It is
    here for the first gauge anyone adds - Celery in-progress task counts being
    the obvious candidate - at which point it is load-bearing and its absence
    would be a slow, quiet drift rather than a visible failure.

    Gunicorn calls this in the arbiter on child exit, the only place with both
    the pid and the knowledge that it is gone.

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

    Args:
        server: The gunicorn Arbiter instance.
        worker: The freshly forked worker process.
    """
    from psycogreen.gevent import patch_psycopg

    patch_psycopg()
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
