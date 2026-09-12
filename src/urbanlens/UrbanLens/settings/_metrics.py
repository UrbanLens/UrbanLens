"""Which processes register Prometheus instrumentation."""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

#: ``UL_PROCESS_ROLE`` values whose stack is actually scraped.
INSTRUMENTED_PROCESS_ROLES = frozenset({"web", "unspecified"})


def instrumentation_wanted(*, metrics_enabled: bool, process_role: str) -> bool:
    """Return True when metrics are on and this process is scraped."""
    return metrics_enabled and process_role in INSTRUMENTED_PROCESS_ROLES


def require_django_prometheus() -> None:
    """Fail when metrics are on but django-prometheus is missing."""
    try:
        import django_prometheus  # noqa: F401  (imported for its side effect of proving it is installed)
    except ImportError as exc:
        raise ImproperlyConfigured(
            "UL_METRICS_ENABLED is on, but django-prometheus is not installed in this image. "
            "Either rebuild the image against the current pyproject.toml, or set UL_METRICS_ENABLED=false "
            "until you can. Metrics instrumentation is the only thing that needs the package."
        ) from exc


def disable_multiprocess_metrics() -> None:
    """Take ``prometheus_client`` out of multiprocess mode.

    Re-resolves ValueClass after popping the env vars, so the pop applies
    even when the library was already imported.
    """
    for name in ("PROMETHEUS_MULTIPROC_DIR", "prometheus_multiproc_dir"):
        os.environ.pop(name, None)
    # Re-resolves to the single-process class either way.
    from prometheus_client import values

    values.ValueClass = values.get_value_class()
