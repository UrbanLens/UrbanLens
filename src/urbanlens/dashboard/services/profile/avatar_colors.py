"""Deterministic, collision-avoiding background colors for fallback (initial) avatars.

A profile with no uploaded photo renders as a plain colored circle with its
first initial (``.msg-avatar-initial``). Every one of those circles used the
exact same fixed color everywhere, which is fine for a single avatar shown
alone but makes several of them indistinguishable the moment they appear
together in one list - most notably a group chat's member dialog, where
every member with no avatar looked like the same person.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Must match the number of `.avatar-color-N` classes defined in _messages.scss.
PALETTE_SIZE = 10


def _preferred_color_index(identity: str) -> int:
    """Stable hash of an identity string into a palette slot.

    Args:
        identity: A stable per-person string (e.g. profile slug).

    Returns:
        An index in ``range(PALETTE_SIZE)``.
    """
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return digest[0] % PALETTE_SIZE


def assign_avatar_colors[T](items: Sequence[T], *, identity: Callable[[T], str], attr: str = "avatar_color_class") -> None:
    """Attach an ``avatar_color_class`` to each item, distinct within this list.

    Each item's *preferred* slot is a stable hash of its identity string, so
    the same person tends to land on the same color across different
    renders. When two items in this same list would collide on that
    preferred slot, later ones shift to the next free slot instead -
    guaranteeing nobody in one rendered list shares a color with anyone else
    in that same list (as long as the list has at most ``PALETTE_SIZE``
    people; past that, colors necessarily start repeating).

    Args:
        items: The items being rendered together (e.g. a group's member list).
            Mutated in place - each gets ``attr`` set to a CSS class name.
        identity: Returns a stable identity string for an item (e.g. profile slug).
        attr: Attribute name to set on each item. Defaults to ``avatar_color_class``.
    """
    used: set[int] = set()
    for item in items:
        preferred = _preferred_color_index(identity(item))
        slot = preferred
        offset = 0
        while slot in used and offset < PALETTE_SIZE:
            offset += 1
            slot = (preferred + offset) % PALETTE_SIZE
        used.add(slot)
        setattr(item, attr, f"avatar-color-{slot}")
