"""Trigger side effects when specific LocationCache sources are (re)written."""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.cache.location_cache import LocationCache


@receiver(post_save, sender=LocationCache, dispatch_uid="location_cache_seed_articles_from_wikipedia")
def seed_articles_on_wikipedia_cache_write(sender: type[LocationCache], instance: LocationCache, created: bool = False, **kwargs) -> None:
    """Seed articles and add the matched Wikipedia link whenever a location's Wikipedia match is (re)cached.

    Fires on every write to a location's "wikipedia" LocationCache row, since
    ``LocationCache.set`` always upserts via ``update_or_create`` regardless
    of whether a row already existed - but the per-pin seeding loop below
    only runs when ``created`` is True, i.e. the first time this location's
    Wikipedia match is cached. Every pin on a popular location has already
    been seeded by that first run, so re-walking all of them (an unbounded,
    O(pins) loop) on every later re-fetch/refresh would be pure waste; the
    per-pin seeding (``seed_pin_article_from_wikipedia``) and link adding
    (``add_pin_link``) are themselves idempotent, but that doesn't help when
    the loop itself, not any individual seed call, is the cost.

    The single, O(1) wiki-level calls (``seed_wiki_article_from_wikipedia``,
    ``add_wiki_link``) stay unconditional - they aren't the flagged
    unbounded cost, and a wiki can be created for the location after its
    Wikipedia cache row already exists, in which case a later re-fetch is
    exactly what lets ``add_wiki_link`` finally attach the link.

    Args:
        sender: The model class.
        instance: The LocationCache row that was just saved.
        created: True if this write created the row (first cache of this
            source for this location); False if it updated an existing row.
        **kwargs: Additional keyword arguments.
    """
    if instance.source != "wikipedia":
        return

    def _run() -> None:
        from django.core.exceptions import ObjectDoesNotExist

        from urbanlens.dashboard.services.locations.external_links import add_pin_link, add_wiki_link
        from urbanlens.dashboard.services.wiki_seed import seed_pin_article_from_wikipedia, seed_wiki_article_from_wikipedia

        location = instance.location
        url = (instance.data or {}).get("url") or ""
        link_name = "Wikipedia"

        seed_wiki_article_from_wikipedia(location)

        if created:
            for pin in location.pins.select_related("profile").all():
                seed_pin_article_from_wikipedia(pin)
                if url:
                    add_pin_link(pin, url, link_name)

        if url:
            try:
                wiki = location.wiki
            except ObjectDoesNotExist:
                wiki = None
            if wiki is not None:
                add_wiki_link(wiki, url, link_name)

    transaction.on_commit(_run)
