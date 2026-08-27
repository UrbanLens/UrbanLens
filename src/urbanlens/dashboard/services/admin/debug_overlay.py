"""Admin-only observability for external-API results on the pin detail page.

Tracks, per rendered result, what query/coordinates produced it and whether
it was served from cache -- surfaced client-side via the Dev Tools toolbar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DebugEntry:
    """One external-API result's provenance, for the admin debug overlay.

    Attributes:
        source: Short identifier for the data source (e.g. ``"wikipedia"``, ``"esri"``).
        query: The search term, address, or coordinates actually used for the lookup.
        from_cache: Whether this result was served from cache rather than a fresh API call.
        count: Number of results the lookup produced, when meaningful (e.g. media
            items, search hits, listings). ``None`` for sources that return a
            single yes/no match (coordinates-only providers) rather than a set.
    """

    source: str
    query: str
    from_cache: bool
    count: int | None = None


class _PermissionCheckableUser(Protocol):
    """Structural type for a user object that supports permission checks.

    Matches both the concrete ``User`` model (via ``PermissionsMixin``) and
    ``AnonymousUser`` without depending on either directly -- plain
    ``AbstractBaseUser`` alone doesn't define ``has_perm``.
    """

    @property
    def is_authenticated(self) -> bool: ...

    def has_perm(self, perm: str) -> bool: ...


def can_view_debug_overlay(user: _PermissionCheckableUser) -> bool:
    """Whether ``user`` may see external-API query/cache-status debug info.

    Args:
        user: The current request user.

    Returns:
        True for authenticated users holding the ``dashboard.view_site_admin``
        permission, regardless of environment.
    """
    return bool(user.is_authenticated and user.has_perm("dashboard.view_site_admin"))
