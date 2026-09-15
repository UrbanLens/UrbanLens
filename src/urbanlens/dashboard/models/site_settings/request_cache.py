"""Request-scoped memoisation for the ``SiteSettings`` singleton and each user's feature set.
``SiteSettings.get_current()`` is called ~80 places, and several of them run on every single page: three separate context processors each fetch it, then the controller fetches it again, then every ``user_has_feature()`` check fetches it once more.
Each call was its own ``get_or_create(pk=1)`` round-trip for a row that cannot change mid-request, so an ordinary map render spent a handful of identical queries on one singleton.
A feature set is the settings row plus the user's subscriptions, and a page asks about it from its views and its template alike, so it is remembered alongside.
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
    _state.features = {}


def end_scope(**_kwargs: object) -> None:
    """Disarm the memo and drop everything remembered (a ``request_finished`` receiver)."""
    _state.enabled = False
    _state.value = None
    _state.features = {}


def invalidate(**_kwargs: object) -> None:
    """Forget the memoised row and every feature set, without disarming the scope.

    Connected to the ``post_save`` of the rows a feature set is built from, so a change made during a request is seen
    by the rest of it.
    """
    _state.value = None
    _state.features = {}


def get_cached() -> SiteSettings | None:
    """Return the memoised settings row, or None if unset or not in an armed scope."""
    if not getattr(_state, "enabled", False):
        return None
    return getattr(_state, "value", None)


def set_cached(value: SiteSettings) -> None:
    """Memoise ``value`` for the rest of this scope, if one is armed."""
    if getattr(_state, "enabled", False):
        _state.value = value


def get_features(user_id: int) -> frozenset[str] | None:
    """Return the user's memoised feature set, or None if unset or not in an armed scope."""
    if not getattr(_state, "enabled", False):
        return None
    return getattr(_state, "features", {}).get(user_id)


def set_features(user_id: int, features: frozenset[str]) -> None:
    """Memoise the user's feature set for the rest of this scope, if one is armed."""
    if getattr(_state, "enabled", False):
        _state.features = {**getattr(_state, "features", {}), user_id: features}
