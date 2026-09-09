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

    ``post_fork`` (above) looks like the obvious place for this, and it was
    the first place it lived. It is the wrong hook, structurally, for
    anything that touches Django's ORM under the gevent worker, and the
    failure it produces is not a crash at boot - it is silent, and shows up
    minutes later as every single request failing.

    Gunicorn's own worker lifecycle (``Arbiter.spawn_worker``) calls
    ``post_fork`` immediately after ``os.fork()``, THEN calls
    ``worker.init_process()`` - which, for ``GeventWorker``, is where
    ``gevent.monkey.patch_all()`` actually runs. ``post_worker_init`` fires
    after ``init_process()`` completes, so anything here runs AFTER that
    patch - the same ordering gunicorn gives an unpatched app importing its
    WSGI module the normal way (``wsgi.py``, loaded even later than this).

    Django's ``ConnectionHandler`` (``django.db.connections``) is
    ``thread_critical = True``, which backs it with a genuine
    ``threading.local()`` (see ``asgiref.local.Local.__init__``) captured
    the FIRST TIME the handler is touched - not per-request, once, for the
    life of the object. Warming the URLconf from ``post_fork`` (via
    ``django.setup()``, which resolves ``django.db.connections`` as part of
    app-registry setup) captured that ``threading.local()`` before
    ``gevent.monkey.patch_all()`` had swapped ``threading.local`` for its
    greenlet-aware replacement - so it stayed a REAL OS-thread-local for the
    rest of the process, even though every real request runs in a greenlet
    that gevent's patch gives its own synthetic thread identity.
    ``connections.close_all()`` in that hook does not fix this: it closes
    the DB-API socket underneath the existing wrapper object, but the
    Python object itself - and the real-OS-thread storage slot Django
    created it in - persists, so the SAME poisoned wrapper is what every
    later greenlet's request finds. The result: EVERY request fails
    ``close_old_connections`` (the ``request_started`` signal receiver) with
    ``django.db.utils.DatabaseError: DatabaseWrapper objects created in a
    thread can only be used in that same thread`` - a permanent,
    100%-reproducible failure of every request the worker ever serves, not
    an occasional race. Measured live against a real build, 2026-09-07:
    ``urbanlens-ws`` (daphne, no gunicorn.conf.py, no gevent monkey-patching
    at all) was healthy on the same pod network the whole time, which is
    what pointed at this file rather than the database or a missing secret.

    Running the identical warm-up here instead - after ``init_process()``'s
    patching - means ``ConnectionHandler``'s first ``threading.local()`` is
    already the gevent-aware one, and behaves exactly as it would if no
    warm-up existed at all: correctly greenlet-scoped from the start.

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

    Called from ``post_worker_init``, not ``post_fork`` - see that hook's own
    docstring for why the choice of hook is load-bearing here, not
    cosmetic: this function itself is unchanged from when it lived in the
    wrong one.

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
