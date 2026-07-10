"""Explicit, user-initiated creation of community Wikis for a pin's Location.

Wikis are never created automatically any more: the user clicks "Create
community wiki" on the pin detail page and chooses which of their pin's fields
(if any) to seed the new wiki with. External enrichment (Google place linking,
name resolution, boundary generation) runs afterwards in a Celery task so pin
creation and bulk imports never touch external APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatVote

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)

#: Security fields shared between Pin and Wiki (both inherit SecurityModel).
SECURITY_FIELDS = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

#: Pin scalar fields a user may copy into a newly created wiki. Keys are the
#: tokens posted by the create-wiki dialog. Aliases and photos are seeded
#: separately (per-item selection, see alias_ids/image_ids on create_for_pin).
SEEDABLE_FIELDS = ("name", "danger", "vulnerability")

#: Stat fields seeded as the pin owner's own initial WikiStatVote, rather than
#: a plain scalar copy - a Wiki has no single "danger"/"vulnerability" value of
#: its own, only the composite of every contributing profile's vote.
_SEEDABLE_VOTE_FIELDS = ("danger", "vulnerability")


@dataclass(slots=True)
class WikiCreationService:
    """Create the community Wiki for a pin's Location, seeded from chosen pin fields."""

    def create_for_pin(
        self,
        pin: Pin,
        *,
        include_fields: set[str] | None = None,
        alias_ids: set[int] | None = None,
        image_ids: set[int] | None = None,
    ) -> tuple[Wiki, bool]:
        """Create (or fetch) the Wiki for a pin's Location and link the pin to it.

        Args:
            pin: The pin whose Location gets a community wiki.
            include_fields: Subset of :data:`SEEDABLE_FIELDS` the user chose to
                copy from their pin into the new wiki. Ignored when the wiki
                already exists (never overwrite community content with
                personal data).
            alias_ids: PKs of the pin's own (non-official) aliases to copy in
                as wiki aliases, on top of official ones (always copied).
            image_ids: PKs of the pin's own photos to also attach to the wiki.

        Returns:
            Tuple of (Wiki, created).

        Raises:
            ValueError: If the pin has no Location to attach a wiki to.
        """
        if pin.location_id is None:
            raise ValueError("Cannot create a wiki for a pin without a Location")

        include = {f for f in (include_fields or set()) if f in SEEDABLE_FIELDS}
        location: Location = pin.location
        defaults: dict = {}
        if "name" in include and pin.name and pin.name.strip():
            defaults["name"] = pin.name.strip()

        with transaction.atomic():
            wiki, created = Wiki.objects.get_or_create_for_location(location, defaults=defaults)
            if created:
                for field in _SEEDABLE_VOTE_FIELDS:
                    if field not in include:
                        continue
                    value = getattr(pin, field, 0)
                    if value:
                        WikiStatVote.objects.update_or_create(wiki=wiki, profile=pin.profile, field=field, defaults={"value": value})
                self._seed_aliases(pin, wiki, alias_ids or set())
                self._seed_photos(pin, wiki, image_ids or set())
            # Link the pin (and any of the user's other pins on this location
            # that aren't linked yet) to the community wiki.
            Pin.objects.filter(pk=pin.pk).update(wiki=wiki)

        def _enqueue() -> None:
            from urbanlens.dashboard.services.celery import safely_enqueue_task
            from urbanlens.dashboard.tasks import enrich_wiki_location

            safely_enqueue_task(enrich_wiki_location, wiki.pk)

        if created:
            transaction.on_commit(_enqueue)
        return wiki, created

    def _seed_aliases(self, pin: Pin, wiki: Wiki, alias_ids: set[int]) -> None:
        """Copy the pin's official aliases (always) plus any chosen extras into the wiki."""
        official = pin.aliases.filter(kind=AliasType.OFFICIAL)
        chosen = pin.aliases.filter(pk__in=alias_ids).exclude(kind=AliasType.OFFICIAL)
        for alias in list(official) + list(chosen):
            WikiAlias.objects.get_or_create(wiki=wiki, name=alias.name, defaults={"kind": alias.kind, "source": alias.source})

    def _seed_photos(self, pin: Pin, wiki: Wiki, image_ids: set[int]) -> None:
        """Attach the chosen photos to the wiki's gallery, keeping their pin link intact."""
        if image_ids:
            pin.images.filter(pk__in=image_ids).update(wiki=wiki)


def seedable_field_values(pin: Pin) -> list[dict]:
    """Describe which pin scalar fields have values worth offering in the create-wiki dialog.

    Args:
        pin: The pin whose fields are candidates for seeding.

    Returns:
        List of dicts with ``field``, ``label`` and a short display ``value``,
        one per seedable field that actually has content on this pin.
    """
    candidates: list[dict] = []
    if pin.name and pin.name.strip():
        candidates.append({"field": "name", "label": "Name", "value": pin.name.strip()})
    if pin.danger:
        candidates.append({"field": "danger", "label": "Danger", "value": f"{pin.danger} / 5"})
    if pin.vulnerability:
        candidates.append({"field": "vulnerability", "label": "Vulnerability", "value": f"{pin.vulnerability} / 5"})
    return candidates


def seedable_aliases(pin: Pin) -> list[PinAlias]:
    """Every alias on the pin, for the create-wiki dialog's per-alias picker.

    Official aliases are included so the dialog can show them (always copied,
    not deselectable); the template distinguishes them via ``alias.kind``.
    """
    return list(pin.aliases.all())


def seedable_photos(pin: Pin) -> list[Image]:
    """The pin's own photos, for the create-wiki dialog's per-photo picker."""
    return list(pin.images.all())
