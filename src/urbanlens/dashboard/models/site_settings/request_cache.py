"""Request-scoped memoisation for the ``SiteSettings`` singleton and each account's standing.
``SiteSettings.get_current()`` is called ~80 places, and several of them run on every single page: three separate context processors each fetch it, then the controller fetches it again, then every ``user_has_feature()`` check fetches it once more.
Each call was its own ``get_or_create(pk=1)`` round-trip for a row that cannot change mid-request, so an ordinary map render spent a handful of identical queries on one singleton.
What an account may see is the settings row plus its subscriptions, and a page asks about it from its views and its template alike, so it is remembered alongside - as an ``AccessState``, which is also cached across requests (see ``models.subscriptions.access_state``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.local import Local

if TYPE_CHECKING:
    from urbanlens.dashboard.models.site_settings.model import SiteSettings
    from urbanlens.dashboard.models.subscriptions.access_state import AccessState

# Task/thread-local rather than thread-local: under Daphne a single thread
# interleaves many requests, and asgiref.local.Local follows the asyncio task.
_state = Local()


def begin_scope(**_kwargs: object) -> None:
    """Arm the memo for the scope that is starting (a ``request_started`` receiver)."""
    _state.enabled = True
    _state.value = None
    _state.access = {}
    _state.access_changed = False


def end_scope(**_kwargs: object) -> None:
    """Disarm the memo and drop everything remembered (a ``request_finished`` receiver)."""
    _state.enabled = False
    _state.value = None
    _state.access = {}
    _state.access_changed = False


def invalidate(**_kwargs: object) -> None:
    """Forget the memoised row and every account's standing, without disarming the scope.

    Connected to the ``post_save`` of the rows these are built from, so a change made during a request is seen
    by the rest of it.
    """
    _state.value = None
    _state.access = {}


def distrust_shared_access(**_kwargs: object) -> None:
    """Record that this scope changed a row access is derived from.

    Dropping the memo is not enough on its own: the next read would fall through to the
    cross-request cache, whose entries were written before the change and are still stamped with
    the current generation, because that bump waits for the commit
    (:func:`~urbanlens.dashboard.models.subscriptions.access_state.forget` says why). The rest of
    the writer's own scope therefore reads from the database instead, and writes nothing back -
    storing what it computed would stamp uncommitted rows as everyone's answer.
    """
    _state.access_changed = True


def shared_access_is_distrusted() -> bool:
    """Whether this scope has changed access and so must not read or write the shared cache."""
    if not getattr(_state, "enabled", False):
        return False
    return bool(getattr(_state, "access_changed", False))


def get_cached() -> SiteSettings | None:
    """Return the memoised settings row, or None if unset or not in an armed scope."""
    if not getattr(_state, "enabled", False):
        return None
    return getattr(_state, "value", None)


def set_cached(value: SiteSettings) -> None:
    """Memoise ``value`` for the rest of this scope, if one is armed."""
    if getattr(_state, "enabled", False):
        _state.value = value


def get_access(user_id: int) -> AccessState | None:
    """Return the user's memoised access state, or None if unset or not in an armed scope."""
    if not getattr(_state, "enabled", False):
        return None
    return getattr(_state, "access", {}).get(user_id)


def set_access(user_id: int, state: AccessState) -> None:
    """Memoise the user's access state for the rest of this scope, if one is armed.

    The state, not the feature set it implies: an admin's features are every feature, which says
    nothing about whether they are an admin, and the dev toolbar asks the second question.
    """
    if getattr(_state, "enabled", False):
        _state.access = {**getattr(_state, "access", {}), user_id: state}
