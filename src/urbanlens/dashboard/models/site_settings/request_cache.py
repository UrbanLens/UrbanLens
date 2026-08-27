"""Request-scoped memoisation for the ``SiteSettings`` singleton.

``SiteSettings.get_current()`` is called ~80 places, and several of them run on
every single page: three separate context processors each fetch it, then the
controller fetches it again, then every ``user_has_feature()`` check fetches it
once more. Each call was its own ``get_or_create(pk=1)`` round-trip for a row
that cannot change mid-request, so an ordinary map render spent a handful of
identical queries on one singleton.

Caching is deliberately **opt-in per scope** rather than global. A process-wide
or TTL cache would be stale in exactly the places that hurt: long-lived Celery
workers would pin a settings row for the life of the worker, and the test suite
mutates settings through ``queryset.update()`` (which bypasses ``save()`` and so
cannot invalidate anything). Instead the memo is armed by ``request_started``
and torn down by ``request_finished``; anywhere without a request - Celery
tasks, management commands, shell - simply never arms it and keeps reading
through to the database as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.local import Local

if TYPE_CHECKING:
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

# Task/thread-local rather than thread-local: under Daphne a single thread
# interleaves many requests, and asgiref.local.Local follows the asyncio task.
_state = Local()


def begin_scope(**_kwargs: object) -> None:
    """Arm the memo for the scope that is starting (a ``request_started`` receiver)."""
    _state.enabled = True
    _state.value = None


def end_scope(**_kwargs: object) -> None:
    """Disarm the memo and drop the cached row (a ``request_finished`` receiver)."""
    _state.enabled = False
    _state.value = None


def invalidate(**_kwargs: object) -> None:
    """Forget any memoised row, without disarming the scope.

    Connected to ``SiteSettings``'s ``post_save`` so an admin editing settings sees
    their own change on the rest of that request, rather than the row they replaced.
    """
    _state.value = None


def get_cached() -> SiteSettings | None:
    """Return the memoised settings row, or None if unset or not in an armed scope."""
    if not getattr(_state, "enabled", False):
        return None
    return getattr(_state, "value", None)


def set_cached(value: SiteSettings) -> None:
    """Memoise ``value`` for the rest of this scope, if one is armed."""
    if getattr(_state, "enabled", False):
        _state.value = value
