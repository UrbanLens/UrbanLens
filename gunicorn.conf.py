"""Gunicorn config for the production ``app`` service.

Loaded via ``-c gunicorn.conf.py``. See package.json for worker flags.
"""

import os
from pathlib import Path


def _multiproc_dir():
    """Return the Prometheus multiprocess dir, if set."""
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

    Args:
        server: The gunicorn Arbiter instance.
        worker: The freshly forked worker process.
    """
    from psycogreen.gevent import patch_psycopg

    patch_psycopg()


def post_worker_init(worker):
    """Warm the URLconf after gevent patching has run."""
    _warm_urlconf(worker)


def _warm_urlconf(worker):
    """Import the URLconf now so the first request does not pay for it."""
    try:
        import django

        django.setup()
        from django.urls import get_resolver

        patterns = get_resolver().url_patterns
        worker.log.info("URLconf warmed: %d root patterns", len(patterns))
    except Exception:
        worker.log.exception("URLconf warm-up failed; continuing without it")
