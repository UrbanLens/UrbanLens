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
