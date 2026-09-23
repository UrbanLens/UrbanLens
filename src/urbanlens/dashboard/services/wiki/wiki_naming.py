"""Name a community wiki from public sources without overriding anyone who named it by hand.

Candidates come from the Location's cached public data (Wikipedia, REData, CRIS, OSM, the
Location's official name, Google as a last resort). A private pin's own name is never one of them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.services.locations.naming import FALLBACK_ONLY_NAME_SOURCES, is_meaningful_name, normalize_name_for_comparison, sanitize_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Alias source for a name taken from ``Location.official_name`` rather than straight from a provider.
OFFICIAL_NAME_SOURCE = "official_name"

#: Alias sources marking a stand-in name that a resolved provider name may still replace.
PROVISIONAL_NAME_SOURCES: frozenset[str] = FALLBACK_ONLY_NAME_SOURCES | {OFFICIAL_NAME_SOURCE}


def name_set_by_person(wiki: Wiki) -> bool:
    """Whether the wiki's current name was last written by a person.

    A wiki with no recorded name write predates revision history; its name is treated as a
    person's when it is meaningful, since nothing says otherwise.

    Args:
        wiki: The wiki to inspect.

    Returns:
        True when the latest ``name`` revision came from a user, or provenance is unknown.
    """
    from urbanlens.dashboard.models.wiki.revision import WikiFieldRevision

    latest = WikiFieldRevision.objects.filter(target_id=wiki.pk, field_name="name").order_by("-id").values_list("source", flat=True).first()
    if latest is None:
        return is_meaningful_name(wiki.name)
    return latest == WriteSource.USER


def is_provisional_name(wiki: Wiki) -> bool:
    """Whether automatic naming may still replace the wiki's name.

    A placeholder always may. So may an automatic stand-in - a Google guess or a creation-time
    official name, or a name written past ``Wiki.save`` and so carrying no alias - until a person
    writes one.

    Args:
        wiki: The wiki to inspect.

    Returns:
        True when the name is a placeholder or an unconfirmed stand-in.
    """
    if not is_meaningful_name(wiki.name):
        return True
    if name_set_by_person(wiki):
        return False
    sources = set(wiki.aliases.filter(name__iexact=wiki.name.strip()).values_list("source", flat=True))
    return not sources or bool(sources & PROVISIONAL_NAME_SOURCES)


def wiki_named_by_location(location: Location) -> Wiki | None:
    """The wiki whose name and aliases this Location's public names may feed.

    The place's wiki, when the Location is the wiki's own or carries a root pin. A Location holding
    only child (building) pins describes one building, so its names stay off the parcel's page.

    Args:
        location: The Location whose cached names are being resolved.

    Returns:
        The wiki, or None.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = Wiki.objects.existing_for_location(location)
    if wiki is None or wiki.location_id == location.pk:
        return wiki
    if location.pins.filter(parent_pin__isnull=True).exists():
        return wiki
    return None


def adopt_public_name(wiki: Wiki, name: str | None, *, source: str) -> bool:
    """Rename the wiki to a public name when its current name is provisional.

    The write is recorded as automatic and the name is kept as an official alias credited to
    ``source``, so the name stays one of the wiki's own aliases and a later, better source can
    recognise a fallback guess. The row is re-read under a lock, so a person renaming it meanwhile
    wins.

    Args:
        wiki: The wiki to rename. Updated in place when renamed.
        name: The candidate name, from a public source only.
        source: The name provider's source slug, e.g. ``"wikipedia"``.

    Returns:
        True when the wiki was renamed.
    """
    from urbanlens.dashboard.models.aliases.model import AliasType, WikiAlias
    from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, WikiAutoRemoval
    from urbanlens.dashboard.models.wiki.model import Wiki

    clean = sanitize_name(name)
    if not is_meaningful_name(clean) or normalize_name_for_comparison(clean) == normalize_name_for_comparison(wiki.name):
        return False
    if WikiAutoRemoval.objects.was_removed(wiki=wiki, kind=AutoRemovalKind.ALIAS, value=clean):
        return False

    with transaction.atomic():
        current = Wiki.objects.select_for_update().filter(pk=wiki.pk).first()
        if current is None or current.name != wiki.name or not is_provisional_name(current):
            return False
        try:
            with transaction.atomic():
                WikiAlias.objects.get_or_create(wiki=current, name__iexact=clean, defaults={"name": clean, "kind": AliasType.OFFICIAL, "source": source})
        except IntegrityError:
            logger.debug("Alias %r for wiki %s already exists", clean, current.pk)
        with writing_as(WriteSource.AUTOMATIC):
            current.name = clean
            current.save(update_fields=["name", "updated"])

    wiki.name = current.name
    return True
