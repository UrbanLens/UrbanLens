"""Trigger side effects when specific LocationCache sources are (re)written."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from urbanlens.dashboard.models.cache.location_cache import LocationCache

logger = logging.getLogger(__name__)

_WIKIPEDIA = "wikipedia"
_WIKIPEDIA_MEDIA = "wikipedia_media"


def _title(data: object) -> str:
    return str(data.get("title") or "") if isinstance(data, dict) else ""


@receiver(pre_save, sender=LocationCache, dispatch_uid="location_cache_remember_previous_wikipedia_title")
def remember_previous_wikipedia_title(sender: type[LocationCache], instance: LocationCache, **kwargs) -> None:
    """Stash the title the row matched before this write, so the post-save hook can tell a new match from a repeat.

    Args:
        sender: The model class.
        instance: The LocationCache row about to be saved.
        **kwargs: Additional keyword arguments.
    """
    previous = sender.objects.filter(pk=instance.pk).values_list("data", flat=True).first() if instance.pk and instance.source == _WIKIPEDIA else None
    instance._previous_wikipedia_title = _title(previous)  # noqa: SLF001


@receiver(post_save, sender=LocationCache, dispatch_uid="location_cache_seed_articles_from_wikipedia")
def seed_articles_on_wikipedia_cache_write(sender: type[LocationCache], instance: LocationCache, created: bool = False, **kwargs) -> None:
    """Seed articles, add the Wikipedia link and refresh names whenever a location's Wikipedia match is (re)cached.

    The wiki's article is seeded on every write (a no-op once one exists). Each pin's article is
    seeded only when this write turns a miss into a match, so an article an owner deleted is not
    recreated by a routine refresh. A new title also drops article images cached for an older one.

    Args:
        sender: The model class.
        instance: The LocationCache row that was just saved.
        created: True if this write created the row.
        **kwargs: Additional keyword arguments.
    """
    if instance.source != _WIKIPEDIA:
        return

    title = _title(instance.data)
    previous_title = "" if created else instance._previous_wikipedia_title  # noqa: SLF001

    def _run() -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.locations.external_links import add_pin_link, add_wiki_link
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources
        from urbanlens.dashboard.services.wiki.wiki_seed import seed_pin_article_from_wikipedia, seed_wiki_article_from_wikipedia

        location = instance.location
        url = (instance.data or {}).get("url") or ""
        link_name = "Wikipedia"

        if title and title != previous_title:
            LocationCache.objects.filter(location=location, source=_WIKIPEDIA_MEDIA).exclude(query_key=title).delete()

        seed_wiki_article_from_wikipedia(location)

        if title and not previous_title:
            for pin in location.pins.select_related("profile").all():
                seed_pin_article_from_wikipedia(pin)
                if url:
                    add_pin_link(pin, url, link_name)

        if url and (wiki := Wiki.objects.existing_for_location(location)) is not None:
            add_wiki_link(wiki, url, link_name)

        if title:
            try:
                update_location_name_from_external_sources(location)
            except Exception:
                logger.exception("Name refresh after a Wikipedia match failed for location %s", location.pk)

    transaction.on_commit(_run)


#: Sources whose rows can carry a historic-register listing containing the point - the highest-ranked name.
_REGISTER_SOURCES = frozenset({"redata_historic_registers", "cris_building_usn"})


@receiver(post_save, sender=LocationCache, dispatch_uid="location_cache_refresh_names_on_register_listing")
def refresh_names_on_register_listing(sender: type[LocationCache], instance: LocationCache, **kwargs) -> None:
    """Refresh a location's names when a register row arrives naming a listing that contains it.

    Register rows land from panel fetches, after the name was first chosen, so this is what lets a
    listing replace an automatic name of a worse tier.

    Args:
        sender: The model class.
        instance: The LocationCache row that was just saved.
        **kwargs: Additional keyword arguments.
    """
    if instance.source not in _REGISTER_SOURCES:
        return

    def _run() -> None:
        from urbanlens.dashboard.services.locations.naming import update_location_name_from_external_sources
        from urbanlens.dashboard.services.locations.register_names import register_listing_names

        location = instance.location
        if not register_listing_names(location):
            return
        try:
            update_location_name_from_external_sources(location)
        except Exception:
            logger.exception("Name refresh after a register listing failed for location %s", location.pk)

    transaction.on_commit(_run)
