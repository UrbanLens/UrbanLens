"""Trigger side effects when specific LocationCache sources are (re)written."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.cache.location_cache import LocationCache

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

_WIKIPEDIA = "wikipedia"
_WIKIPEDIA_MEDIA = "wikipedia_media"


def _title(data: object) -> str:
    return str(data.get("title") or "") if isinstance(data, dict) else ""


@receiver(post_save, sender=LocationCache, dispatch_uid="location_cache_seed_articles_from_wikipedia")
def seed_articles_on_wikipedia_cache_write(sender: type[LocationCache], instance: LocationCache, **kwargs) -> None:
    """
    Seed the wiki's article, add its Wikipedia link and refresh names whenever a location's Wikipedia match is (re)cached.

    The wiki's article is seeded on every write (a no-op once one exists), and neither it nor the link reaches a
    building's wiki (``wiki_seed.takes_wikipedia_article``). A matched title also drops article
    images cached for any other query, including the empty answer cached while an expired match read as none.
    Pins take the match only from their owner's own activity, through
    :func:`~urbanlens.dashboard.services.wiki.wiki_seed.seed_pin_from_cached_wikipedia`.

    Args:
        sender: The model class.
        instance: The LocationCache row that was just saved.
        **kwargs: Additional keyword arguments.
    """
    if instance.source != _WIKIPEDIA:
        return

    title = _title(instance.data)

    def _run() -> None:
        from urbanlens.dashboard.models.links.model import AutoLinkSource
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.locations.external_links import add_wiki_link
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources
        from urbanlens.dashboard.services.wiki.wiki_seed import seed_wiki_article_from_wikipedia, takes_wikipedia_article

        location = instance.location
        url = (instance.data or {}).get("url") or ""

        if title:
            LocationCache.objects.filter(location=location, source=_WIKIPEDIA_MEDIA).exclude(query_key=title).delete()

        seed_wiki_article_from_wikipedia(location)

        if url and (wiki := Wiki.objects.existing_for_location(location)) is not None and takes_wikipedia_article(wiki):
            add_wiki_link(wiki, url, "Wikipedia", source=AutoLinkSource.WIKIPEDIA)

        if title:
            try:
                update_location_name_from_external_sources(location)
            except Exception:
                logger.exception("Name refresh after a Wikipedia match failed for location %s", location.pk)

    transaction.on_commit(_run)


@receiver(post_save, sender=LocationCache, dispatch_uid="location_cache_refresh_names_on_register_listing")
def refresh_names_on_register_listing(sender: type[LocationCache], instance: LocationCache, **kwargs) -> None:
    """
    Refresh a location's names when a register row arrives naming a listing that contains it, or a row names its building.

    Register rows land from panel fetches and site passes, after the name was first chosen, so this is
    what lets a listing replace an automatic name of a worse tier and a building take its own record's name.

    Args:
        sender: The model class.
        instance: The LocationCache row that was just saved.
        **kwargs: Additional keyword arguments.
    """
    from urbanlens.dashboard.services.locations.historic_registers import register_cache_sources

    if instance.source not in register_cache_sources():
        return

    def _run() -> None:
        from urbanlens.dashboard.services.locations.historic_registers import names_a_new_building, register_listing_names
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources

        location = instance.location
        listings = register_listing_names(location)
        if not listings and not names_a_new_building(instance):
            return
        for target in [location, *(_other_root_pin_locations(location) if listings else [])]:
            try:
                update_location_name_from_external_sources(target)
            except Exception:
                logger.exception("Name refresh after a register listing failed for location %s", target.pk)

    transaction.on_commit(_run)


def _other_root_pin_locations(location: Location) -> list[Location]:
    """The property's other Locations holding a root pin, whose names come from the same property."""
    from urbanlens.dashboard.models.location.model import Location

    place = location.place
    if place is None or place.domain_root_id is None:
        return []
    return list(Location.objects.filter(place__domain_root_id=place.domain_root_id, pins__parent_pin__isnull=True).exclude(pk=location.pk).distinct())
