"""Which processes pay for Prometheus instrumentation, and what a missing package looks like.

``UL_METRICS_ENABLED`` is an operator switch that gets flipped on a deployment
that is already running, independently of any image build. Two consequences that
``base.py`` used to get wrong, both handled here:

* The flag reaches **every** process sharing the ``.env`` - app, app-ws, beat and
  all four Celery workers - but only the web process is ever scraped. The rest
  would register a middleware pair whose counters nothing can read.
* An image built before ``django-prometheus`` entered ``pyproject.toml`` dies at
  ``django.setup()`` with a bare ``ModuleNotFoundError`` naming a module the
  operator never heard of, rather than the setting that asked for it.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

#: ``UL_PROCESS_ROLE`` values whose Django stack is actually scraped.
#:
#: ``web`` is the gunicorn service that serves ``/metrics``; ``unspecified`` is
#: what a local checkout, ``runserver`` and the test suite report, where reading
#: the endpoint with ``curl`` is the point. Every other role (``websocket``,
#: ``worker``, ``panels``, ``beat``, ``metrics``, ``sandbox``, ``inference``)
#: either serves no HTTP at all or is never scraped, so instrumenting it buys
#: samples no scraper will collect.
INSTRUMENTED_PROCESS_ROLES = frozenset({"web", "unspecified"})


def instrumentation_wanted(*, metrics_enabled: bool, process_role: str) -> bool:
    """Report whether this process should register django-prometheus.

    Args:
        metrics_enabled: The value of ``UL_METRICS_ENABLED``.
        process_role: The value of ``UL_PROCESS_ROLE``.

    Returns:
        ``True`` only when metrics are on *and* this process is one that can be
        scraped.
    """
    return metrics_enabled and process_role in INSTRUMENTED_PROCESS_ROLES


def require_django_prometheus() -> None:
    """Fail with the setting's name when the package backing it is absent.

    Raises:
        ImproperlyConfigured: When ``django_prometheus`` cannot be imported. The
            message names ``UL_METRICS_ENABLED`` because that is the thing the
            operator changed and the thing they can change back; the underlying
            ``ImportError`` is chained for anyone who needs it.
    """
    try:
        import django_prometheus  # noqa: F401  (imported for its side effect of proving it is installed)
    except ImportError as exc:
        raise ImproperlyConfigured(
            "UL_METRICS_ENABLED is on, but django-prometheus is not installed in this image. "
            "Either rebuild the image against the current pyproject.toml, or set UL_METRICS_ENABLED=false "
            "until you can. Metrics instrumentation is the only thing that needs the package."
        ) from exc
