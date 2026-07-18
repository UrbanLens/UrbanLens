"""Wiki-sync: mirror newly-added aliases between a pin and its community wiki.

Additive only in both directions (never deletes an alias on either side - see
each handler's docstring) and only fires for genuine new aliases, never edits
to an existing one (Profile.sync_aliases' docstring documents this scope
explicitly - "edits to aliases will not be synced" is a deliberate product
decision, not a gap). Both handlers use get_or_create, which is naturally
loop-safe: the mirrored write on the "other side" either creates a row (which
re-fires the opposite handler, but that handler's own get_or_create then finds
the row already exists and does nothing further) or finds one already there.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias

logger = logging.getLogger(__name__)

#: Attribution slug recorded on alias rows created by this sync, distinct from
#: AliasSource.USER (typed directly) so it's clear in the UI/data where a
#: mirrored alias actually came from.
WIKI_SYNC_SOURCE = "wiki_sync"


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

    A location can have many pins (one per user who's pinned it); this fires
    once per opted-in pin, each an independent additive get_or_create. Deleting
    a wiki alias never propagates (no post_delete hook here), matching the
    "additive only" spec.
    """
    if not created:
        return

    def _run() -> None:
        from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
        from urbanlens.dashboard.models.auto_removals.queryset import normalize_auto_removal_value
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.meta import SyncAliasesDirection
        from urbanlens.dashboard.models.wiki.model import Wiki

        try:
            wiki = Wiki.objects.get(pk=instance.wiki_id)
        except Wiki.DoesNotExist:
            return
        pins = Pin.objects.filter(
            location_id=wiki.location_id,
            profile__sync_aliases__in=(SyncAliasesDirection.FROM_WIKI, SyncAliasesDirection.BOTH),
        )
        removed_pin_ids = set(
            PinAutoRemoval.objects.filter(
                pin__in=pins, kind=AutoRemovalKind.ALIAS, value=normalize_auto_removal_value(AutoRemovalKind.ALIAS, instance.name)
            ).values_list("pin_id", flat=True)
        )
        for pin in pins:
            if pin.pk in removed_pin_ids:
                continue
            # Case-insensitive lookup: this pin may already have the name
            # under different casing, which would otherwise race it.
            PinAlias.objects.get_or_create(pin=pin, name__iexact=instance.name, defaults={"name": instance.name, "kind": instance.kind, "source": WIKI_SYNC_SOURCE})

    transaction.on_commit(_run)
