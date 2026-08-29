"""Gunicorn configuration for the production ``app`` service.

Loaded explicitly via ``-c gunicorn.conf.py`` in package.json's ``start``
script. Worker count is not set here: gunicorn reads the ``WEB_CONCURRENCY``
environment variable natively (see docker-compose.yml, where it defaults
to 3).
"""


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
