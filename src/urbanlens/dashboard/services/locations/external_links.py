"""Shared helper for auto-adding a provider-discovered external link to a Pin/Wiki.

Several integrations (Nominatim, EPA ECHO, Wikipedia, ...) each independently
discover one confidently-matched external URL for a pin's location and want
to add it to that pin's (and its wiki's) Links list automatically - without
duplicating an existing entry or resurrecting a link the user deliberately
removed (see ``PinAutoRemoval``/``WikiAutoRemoval``'s tombstone mechanism).
This module is that one shared primitive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki


def add_pin_link(pin: Pin, url: str, name: str) -> bool:
    """Add an external URL to a pin's links, unless already present or previously removed.

    Args:
        pin: The pin whose links should include this URL.
        url: The external URL to add.
        name: The link's display name.

    Returns:
        True when a new ``PinLink`` row was created.
    """
    from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
    from urbanlens.dashboard.models.links.model import PinLink

    if PinAutoRemoval.objects.was_removed(pin=pin, kind=AutoRemovalKind.LINK, value=url):
        return False
    # The exists() check is only a fast path that avoids opening a savepoint for
    # the common "already there" case. The unique constraint on (pin, md5(url))
    # is what actually decides: this runs from a LocationCache signal whose panel
    # fetches are concurrent (their own queue, concurrency 20), so two panels
    # contributing the same URL can both pass the check. The loser's insert is
    # absorbed here rather than escaping as a 500 from inside the signal.
    if PinLink.objects.filter(pin=pin, url=url).exists():
        return False
    try:
        with transaction.atomic():
            PinLink.objects.create(pin=pin, url=url, name=name)
    except IntegrityError:
        return False
    return True


def add_wiki_link(wiki: Wiki, url: str, name: str) -> bool:
    """Add an external URL to a wiki's links, unless already present or previously removed.

    Args:
        wiki: The wiki whose links should include this URL.
        url: The external URL to add.
        name: The link's display name.

    Returns:
        True when a new ``WikiLink`` row was created.
    """
    from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, WikiAutoRemoval
    from urbanlens.dashboard.models.links.model import WikiLink

    if WikiAutoRemoval.objects.was_removed(wiki=wiki, kind=AutoRemovalKind.LINK, value=url):
        return False
    # Fast path only; the (wiki, md5(url)) constraint decides - see add_pin_link.
    if WikiLink.objects.filter(wiki=wiki, url=url).exists():
        return False
    try:
        with transaction.atomic():
            WikiLink.objects.create(wiki=wiki, url=url, name=name)
    except IntegrityError:
        return False
    return True


def add_pin_and_wiki_link(pin: Pin, location: Location, url: str, name: str) -> None:
    """Add an external URL to a pin's links, and to its wiki's links if it has one.

    Args:
        pin: The pin whose links should include this URL.
        location: The pin's location, for reaching its wiki (if any).
        url: The external URL to add.
        name: The link's display name.
    """
    from django.core.exceptions import ObjectDoesNotExist

    add_pin_link(pin, url, name)
    try:
        wiki = location.wiki
    except ObjectDoesNotExist:
        return
    add_wiki_link(wiki, url, name)
