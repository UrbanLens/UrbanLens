"""Admin-only observability for external-API results on the pin detail page.

Tracks, per rendered result, what query/coordinates produced it and whether
it was served from cache -- surfaced client-side via the Dev Tools toolbar.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser, AnonymousUser


@dataclass(frozen=True, slots=True)
class DebugEntry:
    """One external-API result's provenance, for the admin debug overlay.

    Attributes:
        source: Short identifier for the data source (e.g. ``"wikipedia"``, ``"esri"``).
        query: The search term, address, or coordinates actually used for the lookup.
        from_cache: Whether this result was served from cache rather than a fresh API call.
    """

    source: str
    query: str
    from_cache: bool


def can_view_debug_overlay(user: AbstractBaseUser | AnonymousUser) -> bool:
    """Whether ``user`` may see external-API query/cache-status debug info.

    Args:
        user: The current request user.

    Returns:
        True for authenticated users holding the ``dashboard.view_site_admin``
        permission, regardless of environment.
    """
    return bool(user.is_authenticated and user.has_perm("dashboard.view_site_admin"))
