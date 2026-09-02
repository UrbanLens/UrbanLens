"""Sharing a pin's own content to the community Wiki for its Location.

The user picks which of their pin's fields, names and photos to contribute;
nothing here happens without that choice. Publishing private pin content as a
side effect of something else is the defect ``bin/check_pin_not_published_to_wiki.py``
exists for, and a visibility setting is a control over the audience for things
you have shared rather than consent to share them.

The Wiki itself is not created here. Every pinned Location gets one
automatically (``tasks.ensure_wiki_for_location``), enriched in the background,
so by the time anyone shares anything there is already a page to share it to.
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
#: separately (per-item selection, see alias_ids/image_ids on share_from_pin).
#: Name is deliberately excluded: the wiki's name is resolved from external
#: place data (see name-resolution plugin), and the pin's own name already
#: appears as a selectable alias via the Pin.save() name/alias invariant.
SEEDABLE_FIELDS = ("danger", "vulnerability")

#: Stat fields seeded as the pin owner's own initial WikiStatVote, rather than
#: a plain scalar copy - a Wiki has no single "danger"/"vulnerability" value of
#: its own, only the composite of every contributing profile's vote.
_SEEDABLE_VOTE_FIELDS = ("danger", "vulnerability")


@dataclass(slots=True)
class WikiShareService:
    """Copy chosen fields, names and photos from a pin onto its Location's community Wiki."""

    def share_from_pin(
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
                is already official (never overwrite community content with
                personal data) - a still-unofficial draft is fair game, since
                nobody has edited it yet.
            alias_ids: PKs of the pin's own (non-official) aliases to copy in
                as wiki aliases, on top of official ones (always copied).
            image_ids: PKs of the pin's own photos to also attach to the wiki.

        Returns:
            Tuple of (Wiki, shared) - `shared` is whether the caller chose
            anything to contribute.

        Raises:
            ValueError: If the pin has no Location to attach a wiki to.
        """
        if pin.location_id is None:
            raise ValueError("Cannot create a wiki for a pin without a Location")

        include = {f for f in (include_fields or set()) if f in SEEDABLE_FIELDS}
        location: Location = pin.location

        photo_ids = self._processed_photo_ids(pin, image_ids or set())
        shared = bool(include or alias_ids or photo_ids)

        with transaction.atomic():
            # Ordinarily a no-op: the page was created when the pin was, by
            # tasks.ensure_wiki_for_location. It creates here only when that
            # task has not landed yet, which is a race rather than a workflow.
            wiki, created = Wiki.objects.get_or_create_for_location(location)

            # Runs on every share, not just the first. That is the difference
            # between this and the create button it replaced: contributing is
            # something a person does repeatedly, and there is no longer a
            # one-time creation moment to hang it off.
            for field in _SEEDABLE_VOTE_FIELDS:
                if field not in include:
                    continue
                value = getattr(pin, field, 0)
                if value:
                    WikiStatVote.objects.update_or_create(wiki=wiki, profile=pin.profile, field=field, defaults={"value": value})
            self._seed_aliases(pin, wiki, alias_ids or set())
            self._seed_photos(pin, wiki, photo_ids)
            if created:
                # Naming stays a creation-time act. Renaming a page other
                # people read because somebody shared a photo to it would be a
                # side effect nobody asked for.
                self._name_from_pin(pin, wiki, alias_ids or set())
            # Link the pin (and any of the user's other pins on this location
            # that aren't linked yet) to the community wiki.
            Pin.objects.filter(pk=pin.pk).update(wiki=wiki)

            if shared and location.place_id is not None:
                # Grandfathers the sharer permanently - see wiki_access's module
                # docstring, "Engaging with a wiki". Gated on `shared`, not on
                # merely reaching this method: opening the dialog and
                # contributing nothing is not the "shared content...in any
                # capacity" this is meant to catch.
                from urbanlens.dashboard.models.place.model import PlaceAccessGrant

                PlaceAccessGrant.objects.record_engagement(pin.profile, location.place)

        return wiki, shared

    def _name_from_pin(self, pin: Pin, wiki: Wiki, alias_ids: set[int]) -> None:
        """Name a newly-created wiki after the place, not its postal address.

        ``claim_for_location`` names a wiki ``location.official_name``, which for
        a reverse-geocoded location is a street address - so creating a wiki from
        a pin called "HRSH", having explicitly chosen the aliases "Hudson
        Heritage" and "Hudson River State Hospital", produced a wiki titled "83
        Hudson View Dr, Poughkeepsie, NY 12601, USA". That was reported from
        staging, and it discards the two things the user actually told us.

        Only aliases are considered, never ``pin.name``. That is a standing
        decision this service already encodes - "name isn't a seedable field at
        all; the wiki's name comes from external place data, and the pin's name
        already surfaces as an alias" - because a pin name is the user's own
        private label for a place and a wiki is public. The aliases are different:
        the user picked them in this very dialog precisely to hand them over, and
        they are copied onto the wiki either way.

        Preference order is the aliases they chose, then official ones. If none is
        meaningful the claimed name stands - an address beats a placeholder.

        Args:
            pin: The pin the wiki is being created from.
            wiki: The freshly-claimed wiki.
            alias_ids: The aliases the user selected in the dialog.
        """
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        candidates = []
        if alias_ids:
            candidates += [alias.name for alias in pin.aliases.filter(pk__in=alias_ids).exclude(kind=AliasType.OFFICIAL).order_by("pk")]
        candidates += [alias.name for alias in pin.aliases.filter(kind=AliasType.OFFICIAL).order_by("pk")]

        better = next((name for name in candidates if is_meaningful_name(name)), None)
        if better and better != wiki.name:
            wiki.name = better
            wiki.save(update_fields=["name", "updated"])

    def _seed_aliases(self, pin: Pin, wiki: Wiki, alias_ids: set[int]) -> None:
        """Copy the pin's official aliases (always) plus any chosen extras into the wiki."""
        official = pin.aliases.filter(kind=AliasType.OFFICIAL)
        chosen = pin.aliases.filter(pk__in=alias_ids).exclude(kind=AliasType.OFFICIAL)
        for alias in list(official) + list(chosen):
            # Case-insensitive lookup matches the alias uniqueness rule, so two
            # source aliases differing only by case don't race the DB constraint.
            WikiAlias.objects.get_or_create(wiki=wiki, name__iexact=alias.name, defaults={"name": alias.name, "kind": alias.kind, "source": alias.source})

    def _seed_photos(self, pin: Pin, wiki: Wiki, image_ids: set[int]) -> None:
        """Attach the chosen photos to the wiki's gallery, keeping their pin link intact."""
        if image_ids:
            pin.images.filter(pk__in=image_ids).update(wiki=wiki)

    def _processed_photo_ids(self, pin: Pin, image_ids: set[int]) -> set[int]:
        """Selected pin photos whose stored bytes have already completed upload processing.

        A photo's wiki link controls who may fetch its file, so an image is only
        eligible once ``process_image_upload`` has stripped embedded EXIF/location
        metadata from its stored bytes. That parse is decorated ``@untrusted_parse``
        and may only run in the sandbox worker (``queue=SANDBOX_QUEUE``) - never
        inline here in the web process - so an image still missing that pass is
        (re-)enqueued for next time and left out of this share rather than
        processed on the spot.
        """
        if not image_ids:
            return set()

        selected = list(pin.images.filter(pk__in=image_ids).only("pk", "upload_processed_at"))
        processed_ids = {image.pk for image in selected if image.upload_processed_at is not None}
        skipped = {image.pk for image in selected} - processed_ids
        if skipped:
            from urbanlens.dashboard.services.core.celery import safely_enqueue_task
            from urbanlens.dashboard.tasks import process_image_upload

            for image_id in skipped:
                safely_enqueue_task(process_image_upload, image_id)
            logger.warning("wiki_share: skipped %d selected photo(s) on pin %s pending upload processing", len(skipped), pin.pk)
        return processed_ids


def seedable_field_values(pin: Pin) -> list[dict]:
    """Describe which pin scalar fields have values worth offering in the create-wiki dialog.

    Args:
        pin: The pin whose fields are candidates for seeding.

    Returns:
        List of dicts with ``field``, ``label`` and a short display ``value``,
        one per seedable field that actually has content on this pin.
    """
    candidates: list[dict] = []
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
