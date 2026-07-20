"""Trigger side effects when specific LocationCache sources are (re)written."""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.cache.location_cache import LocationCache


@receiver(post_save, sender=LocationCache, dispatch_uid="location_cache_seed_wiki_article_from_wikipedia")
def seed_wiki_article_on_wikipedia_cache_write(sender: type[LocationCache], instance: LocationCache, **kwargs) -> None:
    """Seed the location's wiki article whenever its Wikipedia match is (re)cached.

    Fires on every write to a location's "wikipedia" LocationCache row - not
    just the first - since ``LocationCache.set`` always upserts via
    ``update_or_create`` regardless of whether a row already existed. The
    actual seeding (``seed_wiki_article_from_wikipedia``) is itself idempotent
    (no-ops once the wiki already has any article), so there's no need to
    distinguish a fresh row from a re-fetched one here.

    Args:
        sender: The model class.
        instance: The LocationCache row that was just saved.
        **kwargs: Additional keyword arguments.
    """
    if instance.source != "wikipedia":
        return

    def _run() -> None:
        from urbanlens.dashboard.services.wiki_seed import seed_wiki_article_from_wikipedia

        seed_wiki_article_from_wikipedia(instance.location)

    transaction.on_commit(_run)
