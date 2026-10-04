"""A pin's child pins' aliases and labels, listed read-only on the parent's page while "child pin details" is on (P16).

Editing stays on each child's own page; the parent only shows what its children carry, the way it already shows
their notes, visits and photos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.urls import reverse

if TYPE_CHECKING:
    from urbanlens.dashboard.models.aliases.model import PinAlias
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.pin.model import Pin


@dataclass(frozen=True)
class ChildListing:
    """One child pin and what it carries.

    Attributes:
        child: The child pin.
        url: Its page.
        items: Its aliases or labels, in display order.
    """

    child: Pin
    url: str
    items: list[PinAlias] | list[Label] = field(default_factory=list)


def _url_of(child: Pin) -> str:
    return reverse("pin.details", kwargs={"pin_slug": child.slug or child.uuid})


def child_alias_listings(pin: Pin) -> list[ChildListing]:
    """Each descendant pin with aliases, and its aliases other than its current name, which its chip already shows.

    Args:
        pin: The parent pin.

    Returns:
        Listings ordered by child name, skipping children with none.
    """
    from urbanlens.dashboard.models.aliases.model import PinAlias
    from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison

    grouped: dict[int, list[PinAlias]] = {}
    children: dict[int, Pin] = {}
    for alias in PinAlias.objects.filter(pin__in=pin.descendants()).select_related("pin__location__wiki").order_by("name"):
        if normalize_name_for_comparison(alias.name) == normalize_name_for_comparison(alias.pin.effective_name):
            continue
        grouped.setdefault(alias.pin_id, []).append(alias)
        children[alias.pin_id] = alias.pin
    return sorted((ChildListing(child=children[pk], url=_url_of(children[pk]), items=items) for pk, items in grouped.items()), key=lambda listing: listing.child.effective_name.casefold())


def child_label_listings(pin: Pin) -> list[ChildListing]:
    """Each descendant pin with labels, and its labels.

    Args:
        pin: The parent pin.

    Returns:
        Listings ordered by child name, skipping children with none.
    """
    from django.db.models import Prefetch

    from urbanlens.dashboard.models.labels.model import Label

    children = pin.descendants().filter(labels__isnull=False).distinct().select_related("location__wiki").prefetch_related(Prefetch("labels", queryset=Label.objects.order_by("name")))
    listings = [ChildListing(child=child, url=_url_of(child), items=list(child.labels.all())) for child in children]
    return sorted(listings, key=lambda listing: listing.child.effective_name.casefold())
