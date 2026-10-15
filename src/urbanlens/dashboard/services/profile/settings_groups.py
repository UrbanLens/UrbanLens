"""Account preferences a plugin keeps outside the profile row, served and restored with the account settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class SettingsGroup:
    """
    A set of preferences a plugin stores in its own rows, read and written with the account settings.

    Attributes:
        name: The group's key in a data export.
        fields: The writable field names the settings API serves.
        read: The current values of ``fields`` for a profile.
        write: Stores the validated values of some of ``fields``, saving what it changes.
        feature: The site feature a field needs before it can be written, or None when any account may write it.
    """

    name: str
    fields: frozenset[str]
    read: Callable[[Profile], dict[str, Any]]
    write: Callable[[Profile, dict[str, Any]], None]
    feature: str | None = None
