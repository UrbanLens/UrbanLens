"""Gunicorn config for the production ``app`` service.

Named explicitly by package.json's ``start`` script - but gunicorn also reads
``gunicorn.conf.py`` out of the working directory when nothing names it, and
that directory is ``/app`` for every service built from this image. So this file
also configures ``ai-inference``, which runs a different WSGI application with
no Django and no ORM. Each hook below therefore checks that it applies to the
process it has landed in, rather than assuming it is the app.

Why ``-k gthread --threads 4`` is on that command line, since package.json
cannot carry a comment (D11). Each request thread keeps its Postgres connection
(``UL_DB_CONN_MAX_AGE`` on the app service), so a request pays no login, and the
connection population is ``workers x threads``. The app logs in as ``ul_web``,
whose ``CONNECTION LIMIT`` lives in ``services/core/database_roles.py``;
``test_connection_budget_wiring`` fails if the thread count outgrows it. A
gevent greenlet's connection ends with its request, so under gevent the same
setting would reuse nothing.

``--backlog 256`` so overflow queues in the kernel rather than being refused.
"""

import os
from pathlib import Path


def _multiproc_dir():
    """Return the Prometheus multiprocess dir, if set.

    Returns:
        A ``Path`` when ``PROMETHEUS_MULTIPROC_DIR`` is set, else ``None``.
    """
    configured = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    return Path(configured) if configured else None


def on_starting(server):
    """Clear stale Prometheus files before workers fork.

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

    Only under the gevent worker. The patch registers a *gevent* wait callback
    on psycopg2, so under any other worker class there is no hub to yield to -
    it is at best inert and at worst a query waiting on a loop nobody runs.
    Both the app and ``ai-inference`` run ``gthread`` and inherit this hook.

    Deliberately does NOT also warm the URLconf (see ``post_worker_init``
    below for why that has to happen later, in a different hook, not just
    later in this same one).

    Args:
        server: The gunicorn Arbiter instance.
        worker: The freshly forked worker process.
    """
    if getattr(server.cfg, "worker_class_str", "") != "gevent":
        return

    from psycogreen.gevent import patch_psycopg

    patch_psycopg()


def post_worker_init(worker):
    """Warm the URLconf once the worker has initialised.

    Args:
        worker: The freshly initialised worker, used for its logger.
    """
    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        # Not the Django app - nothing to warm, and reporting that as a failure
        # puts an ImproperlyConfigured traceback in the boot log of a service
        # that is configured exactly as intended.
        return
    _warm_urlconf(worker)


def _warm_urlconf(worker):
    """Import the URLconf now so the first request does not pay for it.

    Args:
        worker: The freshly forked worker, used for its logger.
    """
    try:
        import django

        django.setup()
        from urbanlens.core.warmup import warm_urlconf

        # Logging the counts both uses the values (so neither half can be
        # optimised away or read as a mistake) and puts proof in the boot log
        # that each ran. See warm_urlconf for what the two halves are.
        patterns, reversible = warm_urlconf()
        worker.log.info("URLconf warmed: %d root patterns, %d reversible names", patterns, reversible)
    except Exception:
        worker.log.exception("URLconf warm-up failed; continuing without it")
