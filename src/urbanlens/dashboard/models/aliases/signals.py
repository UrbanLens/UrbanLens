"""Wiki-sync: mirror newly-added aliases between a pin and its community wiki.
Additive only in both directions (never deletes an alias on either side - see each handler's docstring) and only fires for genuine new aliases, never edits to an existing one (Profile.sync_aliases' docstring documents this scope explicitly - "edits to aliases will not be synced" is a deliberate product decision, not a gap).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.services.core.celery import safely_enqueue_task

logger = logging.getLogger(__name__)

#: Attribution slug recorded on alias rows created by this sync, distinct from
#: AliasSource.USER (typed directly) so it's clear in the UI/data where a
#: mirrored alias actually came from.
WIKI_SYNC_SOURCE = "wiki_sync"


def _drop_name_sensitive_cache(location_id: int | None) -> None:
    """Drop the location's cached lookups a new name could improve.

    A Wikimedia search is by name, so it always goes. A Wikipedia match is kept: the lookup takes the
    first nearby article that fits, so another name only helps where it missed - and article images
    go with the miss, since they are read from the matched article.

    Args:
        location_id: The location whose cache rows to drop; None is a no-op.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if location_id is None:
        return
    rows = LocationCache.objects.filter(location_id=location_id)
    rows.filter(source="wikimedia").delete()
    if not rows.filter(source="wikipedia", data__has_key="title").exclude(data__title="").exists():
        rows.filter(source__in=("wikipedia", "wikipedia_media")).delete()


@receiver(post_save, sender=PinAlias, dispatch_uid="pin_alias_sync_to_wiki")
def sync_pin_alias_to_wiki(sender: type[PinAlias], instance: PinAlias, created: bool, **kwargs) -> None:
    """Mirror a newly-added pin alias onto the pin's wiki, if the owner opted in.

    Deleting a pin alias never propagates (see controllers.aliases.PinAliasDeleteView -
    no signal hooks post_delete here on purpose), matching the "additive only" spec.
    """
    if not created:
        return

    def _run() -> None:
        from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, WikiAutoRemoval
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.meta import SyncAliasesDirection

        try:
            pin = Pin.objects.select_related("profile").get(pk=instance.pin_id)
        except Pin.DoesNotExist:
            return
        if pin.wiki_id is None:
            return
        if pin.profile.sync_aliases not in (SyncAliasesDirection.TO_WIKI, SyncAliasesDirection.BOTH):
            return
        if WikiAutoRemoval.objects.was_removed(wiki_id=pin.wiki_id, kind=AutoRemovalKind.ALIAS, value=instance.name):
            return
        # Case-insensitive lookup: the mirrored wiki may already have this
        # name under different casing (its own uniqueness is case-insensitive
        # too, but independent of PinAlias's), which would otherwise race it.
        WikiAlias.objects.get_or_create(
            wiki_id=pin.wiki_id,
            name__iexact=instance.name,
            defaults={"name": instance.name, "kind": instance.kind, "source": WIKI_SYNC_SOURCE, "created_by_id": pin.profile_id},
        )

    transaction.on_commit(_run)


@receiver(post_save, sender=WikiAlias, dispatch_uid="wiki_alias_sync_to_pins")
def sync_wiki_alias_to_pins(sender: type[WikiAlias], instance: WikiAlias, created: bool, **kwargs) -> None:
    """Mirror a newly-added wiki alias onto every opted-in profile's pin at that location.

    A location can have many pins, so this is sized by the place's popularity rather than the
    alias author's own data - hence the bulk queue (services.aliases.fanout) rather than inline
    work. Deleting a wiki alias never propagates (no post_delete hook), matching "additive only".
    """
    if not created:
        return

    def _run() -> None:
        from urbanlens.dashboard.tasks import fan_out_wiki_alias_to_pins

        safely_enqueue_task(fan_out_wiki_alias_to_pins, instance.pk)

    transaction.on_commit(_run)


@receiver(post_save, sender=PinAlias, dispatch_uid="pin_alias_invalidate_name_sensitive_cache")
def invalidate_name_sensitive_cache_for_new_pin_alias(sender: type[PinAlias], instance: PinAlias, created: bool, **kwargs) -> None:
    """Drop the location's Wikipedia/Wikimedia LocationCache rows when a new pin alias appears.

    A missing row is treated as "never queried" (see LocationCache's own
    docstring), so this just makes the next panel view do a fresh lookup with
    the wider name set - it doesn't change any other read path's behavior.
    """
    if not created:
        return

    def _run() -> None:
        from urbanlens.dashboard.models.pin.model import Pin

        try:
            pin = Pin.objects.get(pk=instance.pin_id)
        except Pin.DoesNotExist:
            return
        if pin.location_id is None:
            return
        _drop_name_sensitive_cache(pin.location_id)

    transaction.on_commit(_run)


@receiver(post_save, sender=WikiAlias, dispatch_uid="wiki_alias_invalidate_name_sensitive_cache")
def invalidate_name_sensitive_cache_for_new_wiki_alias(sender: type[WikiAlias], instance: WikiAlias, created: bool, **kwargs) -> None:
    """Drop the location's Wikipedia/Wikimedia LocationCache rows when a new wiki alias appears.

    Same rationale as ``invalidate_name_sensitive_cache_for_new_pin_alias``.
    """
    if not created:
        return

    def _run() -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki

        try:
            wiki = Wiki.objects.get(pk=instance.wiki_id)
        except Wiki.DoesNotExist:
            return
        _drop_name_sensitive_cache(wiki.location_id)

    transaction.on_commit(_run)
