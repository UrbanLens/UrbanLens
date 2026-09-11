"""Mirror one wiki alias onto every opted-in pin at that location.

A location has one pin per person who saved it, so this walks a list whose
length is set by how popular the place is - not by anything the person adding
the alias owns. It used to run inside the committing request, which made one
person's edit cost everyone else's latency (N21 H25); it now runs on the bulk
queue, and this module is the body the task calls.

Iterated rather than materialised, and each row written with ``get_or_create``
rather than in bulk, because a ``PinAlias`` write fires two further receivers
(the mirror back to the wiki, and the name-sensitive cache invalidation) that
the fan-out still needs. Every write is idempotent, so a run that dies halfway
is simply re-run.
"""

from __future__ import annotations

import logging

from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias

logger = logging.getLogger(__name__)

#: Rows held in memory at a time. The list is as long as the place is popular.
_CHUNK = 500


def mirror_wiki_alias_to_pins(alias_id: int) -> int:
    """Give every opted-in pin at this alias's location the same name.

    Args:
        alias_id: Primary key of the ``WikiAlias`` that was just created. Read
            fresh rather than passed whole, so the task argument stays a single
            integer and a deleted alias is simply a no-op.

    Returns:
        How many pins were considered. Pins that already had the name, or whose
        owner removed it before, are counted but not written.
    """
    from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
    from urbanlens.dashboard.models.auto_removals.queryset import normalize_auto_removal_value
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.meta import SyncAliasesDirection

    try:
        instance = WikiAlias.objects.select_related("wiki").get(pk=alias_id)
    except WikiAlias.DoesNotExist:
        return 0
    if instance.wiki is None or instance.wiki.location_id is None:
        return 0

    pins = Pin.objects.filter(
        location_id=instance.wiki.location_id,
        profile__sync_aliases__in=(SyncAliasesDirection.FROM_WIKI, SyncAliasesDirection.BOTH),
    )
    removed_pin_ids = set(
        PinAutoRemoval.objects.filter(
            pin__in=pins,
            kind=AutoRemovalKind.ALIAS,
            value=normalize_auto_removal_value(AutoRemovalKind.ALIAS, instance.name),
        ).values_list("pin_id", flat=True)
    )

    considered = 0
    for pin in pins.iterator(chunk_size=_CHUNK):
        considered += 1
        if pin.pk in removed_pin_ids:
            continue
        # Case-insensitive lookup: this pin may already have the name under
        # different casing, which would otherwise race it.
        PinAlias.objects.get_or_create(
            pin=pin,
            name__iexact=instance.name,
            defaults={"name": instance.name, "kind": instance.kind, "source": _source()},
        )
    return considered


def _source() -> str:
    from urbanlens.dashboard.models.aliases.signals import WIKI_SYNC_SOURCE

    return WIKI_SYNC_SOURCE
