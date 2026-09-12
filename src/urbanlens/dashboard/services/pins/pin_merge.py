"""Merges two of a profile's own pins into one, consolidating every relation.
Distinct from ``controllers.pin_bulk.PinBulkMergeView`` (the map multi-select "Merge" button), which only re-parents pins as children - both rows survive, untouched, just nested."""

from __future__ import annotations

import collections
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.auto_removals.model import PinAutoRemoval
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.custom_fields.model import CustomFieldValue
from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.link_extraction.model import LinkExtraction
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer, MarkupMap, PinMarkup
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_list.model import PinListItem
from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion
from urbanlens.dashboard.models.pin_share.meta import PinShareOrigin, PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion
from urbanlens.dashboard.models.property_owner.model import PinPropertySale
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.trips.model import TripActivity
from urbanlens.dashboard.models.visits.model import PinVisit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import Model

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.floorplans.model import FloorplanMarker
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class UnresolvedMergeConflictError(ValueError):
    """Raised when merge_pins is called without a resolution for a real conflict.

    Attributes:
        keys: The MergeFieldConflict.key values that had no resolution supplied.
    """

    def __init__(self, keys: list[str]) -> None:
        """Store the unresolved keys and build a descriptive message."""
        self.keys = keys
        super().__init__(f"Unresolved merge conflicts: {', '.join(keys)}")


class PinMergeCollisionError(ValueError):
    """Raised when a merge cannot proceed without destroying data."""


class SurvivorRelocationCollisionError(PinMergeCollisionError):
    """The survivor is one of loser's direct children and must move to loser's own parent - avoiding being deleted with it - but another top-level pin already occupies that location.
    Only possible when loser's parent is None, i.e. the survivor would become a new top-level pin."""


class ChildDetachCollisionError(PinMergeCollisionError):
    """A child of loser has to be detached to top level - re-parenting it under the survivor would close a loop - but another top-level pin already occupies its location."""


@dataclass(frozen=True, slots=True)
class MergeFieldConflict:
    """One place two pins hold genuinely different data for the same slot.
    Only the three relations where both sides can hold distinct, real content for one slot ever produce one of these - see the module docstring's "ask the user" bucket."""

    key: str
    label: str
    pin_a_summary: str
    pin_b_summary: str


def _get_article(pin: Pin) -> Article | None:
    """The pin's Article, or None - safe against the reverse OneToOne's DoesNotExist."""
    return getattr(pin, "article", None)


def _get_floorplan_marker(pin: Pin) -> FloorplanMarker | None:
    """The pin's FloorplanMarker twin, or None - safe against the reverse OneToOne's DoesNotExist."""
    return getattr(pin, "floorplan_marker", None)


@dataclass(frozen=True, slots=True)
class _PinConflictData:
    """The three relations a merge can collide on, for one pin."""

    article: Article | None
    boundaries: dict[str, Boundary]
    values: dict[int, CustomFieldValue]


#: What an unsaved pin has: none of the three relations, so nothing to collide
#: with. Read-only, and never mutated by the comparison below.
_NOTHING = _PinConflictData(article=None, boundaries={}, values={})


def _conflict_data(pins: Sequence[Pin]) -> dict[int, _PinConflictData]:
    """Everything :func:`plan_merge_conflicts` compares, for many pins at once.
    The dict-per-pin collapse is safe because both relations are unique per key - ``boundary_unique_pin`` over ``(pin, boundary_type)`` and ``db_cfv_unique_pin`` over ``(field, pin)`` - which is the assumption the comparison already made.

    Args:
        pins: The pins to fetch for.

    Returns:
        One entry per distinct saved pin id."""
    from urbanlens.dashboard.models.article.model import Article as ArticleModel

    pin_ids = {pin.pk for pin in pins if pin.pk is not None}
    if not pin_ids:
        return {}

    articles = {article.pin_id: article for article in ArticleModel.objects.filter(pin_id__in=pin_ids)}
    boundaries: collections.defaultdict[int, dict[str, Boundary]] = collections.defaultdict(dict)
    # Safe only while nothing below reads a deferred column; a summary that wanted the geometry
    # would load it per row and put the fan-out back.
    for boundary in Boundary.objects.filter(pin_id__in=pin_ids).only("pin", "boundary_type", "updated"):
        boundaries[boundary.pin_id][boundary.boundary_type] = boundary
    values: collections.defaultdict[int, dict[int, CustomFieldValue]] = collections.defaultdict(dict)
    for value in CustomFieldValue.objects.filter(pin_id__in=pin_ids).select_related("field"):
        values[value.pin_id][value.field_id] = value

    return {pin_id: _PinConflictData(articles.get(pin_id), boundaries[pin_id], values[pin_id]) for pin_id in pin_ids}


def _conflicts_between(pin_a: Pin, pin_b: Pin, data: dict[int, _PinConflictData]) -> list[MergeFieldConflict]:
    """The comparison half of :func:`plan_merge_conflicts`, over fetched data.

    Args:
        pin_a: One pin under consideration.
        pin_b: The other pin under consideration.
        data: Output of :func:`_conflict_data` covering both pins.

    Returns:
        The conflicts between them."""
    conflicts: list[MergeFieldConflict] = []
    # An unsaved pin has no entry, and correctly has no conflicts: it can hold none of the three
    # relations.
    # Django refuses it before that anyway - a related filter on an unsaved instance raises - so
    # this is about answering rather than crashing, not about permitting something new.
    side_a, side_b = data.get(pin_a.pk, _NOTHING), data.get(pin_b.pk, _NOTHING)

    article_a, article_b = side_a.article, side_b.article
    if article_a is not None and article_b is not None:
        conflicts.append(
            MergeFieldConflict(
                key="article",
                label="Both pins have an article",
                pin_a_summary=f"{article_a.word_count()} words, updated {article_a.updated.date().isoformat()}",
                pin_b_summary=f"{article_b.word_count()} words, updated {article_b.updated.date().isoformat()}",
            ),
        )

    for boundary_type in sorted(set(side_a.boundaries) & set(side_b.boundaries)):
        conflicts.append(
            MergeFieldConflict(
                key=f"boundary:{boundary_type}",
                label=f"Both pins have a {boundary_type} boundary",
                pin_a_summary=f"Updated {side_a.boundaries[boundary_type].updated.date().isoformat()}",
                pin_b_summary=f"Updated {side_b.boundaries[boundary_type].updated.date().isoformat()}",
            ),
        )

    for field_id in sorted(set(side_a.values) & set(side_b.values)):
        value_a, value_b = side_a.values[field_id], side_b.values[field_id]
        conflicts.append(
            MergeFieldConflict(
                key=f"custom_field:{field_id}",
                label=f'Both pins have a value for "{value_a.field.name}"',
                pin_a_summary=value_a.display_value or "(empty)",
                pin_b_summary=value_b.display_value or "(empty)",
            ),
        )

    return conflicts


def plan_merge_conflicts(pin_a: Pin, pin_b: Pin) -> list[MergeFieldConflict]:
    """Every field where pin_a and pin_b both hold real, possibly-divergent data.

    Args:
        pin_a: One pin under consideration.
        pin_b: The other pin under consideration.

    Returns:
        Conflicts the accepting user must resolve before ``merge_pins`` will merge these two pins - empty when nothing needs a decision."""
    return _conflicts_between(pin_a, pin_b, _conflict_data([pin_a, pin_b]))


def plan_merge_conflicts_bulk(pairs: Sequence[tuple[Pin, Pin]]) -> dict[tuple[int, int], list[MergeFieldConflict]]:
    """Conflicts for many pairs, in three queries for the whole batch.
    A caller that then *merges* must re-plan per pair with :func:`plan_merge_conflicts`, because each merge changes what the next one collides with.

    Args:
        pairs: ``(pin_a, pin_b)`` pairs, in any order.

    Returns:
        Conflicts keyed by ``(pin_a.pk, pin_b.pk)``."""
    data = _conflict_data([pin for pair in pairs for pin in pair])
    return {(pin_a.pk, pin_b.pk): _conflicts_between(pin_a, pin_b, data) for pin_a, pin_b in pairs}


def _save_within_savepoint(instance: Model, update_fields: list[str]) -> bool:
    """Save a row that may collide with a uniqueness constraint, recoverably.

    Args:
        instance: The model instance being reassigned onto the survivor.
        update_fields: Fields to write, passed straight to ``save()``.

    Returns:
        True when the row was written; False when it collided and the caller should apply its own dedup rule."""
    try:
        with transaction.atomic():
            instance.save(update_fields=update_fields)
    except IntegrityError:
        return False
    return True


def _reparent_children(survivor: Pin, loser: Pin) -> None:
    """Re-parent loser's child pins onto survivor, skipping any that would create a cycle."""
    for child in list(loser.detail_pins.all()):
        if child.pk == survivor.pk:
            survivor.parent_pin = loser.parent_pin
            if not _save_within_savepoint(survivor, ["parent_pin", "updated"]):
                raise SurvivorRelocationCollisionError(
                    f"Pin merge blocked: survivor pin {survivor.pk} is loser pin {loser.pk}'s direct child and must relocate to loser.parent_pin_id={loser.parent_pin_id} to avoid CASCADE deletion, but a top-level pin already occupies that location.",
                )
            continue
        if child.would_create_cycle(survivor):
            logger.warning("Pin merge: detaching child pin %s to root - re-parenting under the survivor would create a cycle.", child.pk)
            child.parent_pin = None
            if not _save_within_savepoint(child, ["parent_pin", "updated"]):
                # It stays parented to the pin about to be deleted, and Pin.parent_pin CASCADEs - so
                # this child, and the survivor somewhere beneath it, would both be destroyed by the
                # delete this detach exists to prevent.
                # Refuse the merge instead.
                raise ChildDetachCollisionError(
                    f"Pin merge blocked: child pin {child.pk} of loser pin {loser.pk} must detach to top level (survivor pin {survivor.pk} sits beneath it) to avoid a cycle, but a top-level pin already occupies its location.",
                )
            continue
        child.parent_pin = survivor
        child.save(update_fields=["parent_pin", "updated"])


def _merge_aliases(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's aliases onto survivor, dropping case-insensitive duplicates."""
    survivor_names = {alias.name.casefold() for alias in survivor.aliases.all()}
    for alias in list(loser.aliases.all()):
        if alias.name.casefold() in survivor_names:
            alias.delete()
            continue
        alias.pin = survivor
        if _save_within_savepoint(alias, ["pin", "updated"]):
            survivor_names.add(alias.name.casefold())
        else:
            alias.delete()


def _merge_owners(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's owners onto survivor, dropping case-insensitive duplicates."""
    survivor_names = {owner.name.casefold() for owner in survivor.owners.all()}
    for owner in list(loser.owners.all()):
        if owner.name.casefold() in survivor_names:
            owner.delete()
            continue
        owner.pin = survivor
        if _save_within_savepoint(owner, ["pin", "updated"]):
            survivor_names.add(owner.name.casefold())
        else:
            owner.delete()


def _merge_reviews(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's reviews onto survivor; when the same profile reviewed both, keep the newer one."""
    for review in list(Review.objects.filter(pin=loser)):
        existing = Review.objects.filter(pin=survivor, profile_id=review.profile_id).first()
        if existing is not None:
            if review.updated > existing.updated:
                existing.rating = review.rating
                existing.save(update_fields=["rating", "updated"])
            review.delete()
            continue
        review.pin = survivor
        if not _save_within_savepoint(review, ["pin", "updated"]):
            review.delete()


def _merge_list_items(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's list memberships onto survivor, dropping duplicate memberships in the same list."""
    survivor_list_ids = set(PinListItem.objects.filter(pin=survivor).values_list("pin_list_id", flat=True))
    for item in list(PinListItem.objects.filter(pin=loser)):
        if item.pin_list_id in survivor_list_ids:
            item.delete()
            continue
        item.pin = survivor
        if _save_within_savepoint(item, ["pin", "updated"]):
            survivor_list_ids.add(item.pin_list_id)
        else:
            item.delete()


def _merge_auto_removals(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's auto-removal tombstones onto survivor, deduping identical (kind, value) pairs."""
    survivor_keys = set(PinAutoRemoval.objects.filter(pin=survivor).values_list("kind", "value"))
    for removal in list(PinAutoRemoval.objects.filter(pin=loser)):
        key = (removal.kind, removal.value)
        if key in survivor_keys:
            removal.delete()
            continue
        removal.pin = survivor
        if _save_within_savepoint(removal, ["pin", "updated"]):
            survivor_keys.add(key)
        else:
            removal.delete()


def _merge_shares(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's shares onto survivor, dropping duplicate pending/detected shares to the same recipient."""
    survivor_pending = set(PinShare.objects.filter(pin=survivor, status=PinShareStatus.PENDING).values_list("to_profile_id", flat=True))
    survivor_detected = set(PinShare.objects.filter(pin=survivor, origin=PinShareOrigin.MAP_DETECTED).values_list("to_profile_id", flat=True))
    for share in list(PinShare.objects.filter(pin=loser)):
        is_duplicate = (share.status == PinShareStatus.PENDING and share.to_profile_id in survivor_pending) or (share.origin == PinShareOrigin.MAP_DETECTED and share.to_profile_id in survivor_detected)
        if is_duplicate:
            share.delete()
            continue
        share.pin = survivor
        if not _save_within_savepoint(share, ["pin", "updated"]):
            share.delete()
            continue
        if share.status == PinShareStatus.PENDING:
            survivor_pending.add(share.to_profile_id)
        if share.origin == PinShareOrigin.MAP_DETECTED:
            survivor_detected.add(share.to_profile_id)


def _merge_custom_field_values(survivor: Pin, loser: Pin, resolutions: dict[str, int]) -> None:
    """Reassign loser's custom field values onto survivor, applying the user's choice for any conflicting field."""
    survivor_field_ids = set(CustomFieldValue.objects.filter(pin=survivor).values_list("field_id", flat=True))
    for value in list(CustomFieldValue.objects.filter(pin=loser)):
        if value.field_id in survivor_field_ids:
            if resolutions.get(f"custom_field:{value.field_id}") == loser.pk:
                CustomFieldValue.objects.filter(pin=survivor, field_id=value.field_id).delete()
                value.pin = survivor
                value.save(update_fields=["pin", "updated"])
            else:
                value.delete()
            continue
        value.pin = survivor
        value.save(update_fields=["pin", "updated"])
        survivor_field_ids.add(value.field_id)


def _merge_boundaries(survivor: Pin, loser: Pin, resolutions: dict[str, int]) -> None:
    """Reassign loser's boundaries onto survivor, applying the user's choice for any conflicting boundary type."""
    survivor_types = {boundary.boundary_type: boundary for boundary in Boundary.objects.filter(pin=survivor)}
    for boundary in list(Boundary.objects.filter(pin=loser)):
        if boundary.boundary_type in survivor_types:
            if resolutions.get(f"boundary:{boundary.boundary_type}") == loser.pk:
                survivor_types[boundary.boundary_type].delete()
                boundary.pin = survivor
                boundary.save(update_fields=["pin", "updated"])
            else:
                boundary.delete()
            continue
        boundary.pin = survivor
        boundary.save(update_fields=["pin", "updated"])
        survivor_types[boundary.boundary_type] = boundary


def _merge_article(survivor: Pin, loser: Pin, resolutions: dict[str, int]) -> None:
    """Keep one pin's Article when both have one (per resolutions["article"]), else reassign the loser's."""
    loser_article = _get_article(loser)
    if loser_article is None:
        return
    survivor_article = _get_article(survivor)
    if survivor_article is None:
        loser_article.pin = survivor
        loser_article.save(update_fields=["pin", "updated"])
        return
    if resolutions.get("article") == loser.pk:
        survivor_article.delete()
        loser_article.pin = survivor
        loser_article.save(update_fields=["pin", "updated"])
    else:
        loser_article.delete()


def _merge_lineage(survivor: Pin, loser: Pin) -> None:
    """Gap-fill survivor's share lineage/cover photo from loser when survivor has none of its own."""
    update_fields = []
    if survivor.source_share_id is None and loser.source_share_id is not None:
        survivor.source_share_id = loser.source_share_id
        update_fields.append("source_share")
    if survivor.inferred_source_share_id is None and loser.inferred_source_share_id is not None:
        survivor.inferred_source_share_id = loser.inferred_source_share_id
        update_fields.append("inferred_source_share")
    if survivor.cover_photo_id is None and loser.cover_photo_id is not None:
        survivor.cover_photo_id = loser.cover_photo_id
        update_fields.append("cover_photo")
    if update_fields:
        survivor.save(update_fields=[*update_fields, "updated"])


def _repoint_other_merge_suggestions(survivor: Pin, loser: Pin) -> None:
    """Repoint any OTHER pending suggestion mentioning loser onto survivor."""
    for suggestion in PinMergeSuggestion.objects.for_pin(loser):
        if {suggestion.pin_a_id, suggestion.pin_b_id} == {survivor.pk, loser.pk}:
            continue
        if suggestion.pin_a_id == loser.pk:
            suggestion.pin_a = survivor
            suggestion.save(update_fields=["pin_a", "updated"])
        else:
            suggestion.pin_b = survivor
            suggestion.save(update_fields=["pin_b", "updated"])


def _merge_image_attachments(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's image attachments onto survivor, dropping duplicates of an already-attached photo."""
    survivor_image_ids = set(ImageAttachment.objects.filter(pin=survivor).values_list("image_id", flat=True))
    for attachment in list(ImageAttachment.objects.filter(pin=loser)):
        if attachment.image_id in survivor_image_ids:
            attachment.delete()
            continue
        attachment.pin = survivor
        if _save_within_savepoint(attachment, ["pin", "updated"]):
            survivor_image_ids.add(attachment.image_id)
        else:
            attachment.delete()


def _merge_floorplan_marker(survivor: Pin, loser: Pin) -> None:
    """Unlink the loser's floorplan-marker twin rather than repointing it."""
    marker = _get_floorplan_marker(loser)
    if marker is None:
        return
    marker.linked_pin = None
    marker.save(update_fields=["linked_pin", "updated"])


def _merge_albums(survivor: Pin, loser: Pin) -> None:
    """Move the loser's albums onto the survivor, re-slugging on collision.
    ``uq_album_pin_slug`` is unique on ``(parent_pin, slug)``, so a plain reassign fails when both pins have an album with the same slug - two pins each carrying a "Photos" album is the ordinary case, and both hold real images, so neither may be dropped.

    Args:
        survivor: The pin absorbing the albums.
        loser: The pin being merged away."""
    taken = set(Album.objects.filter(parent_pin=survivor).values_list("slug", flat=True))
    for album in Album.objects.filter(parent_pin=loser):
        album.parent_pin = survivor
        if album.slug in taken:
            album.slug = ""
        album.save()
        taken.add(album.slug)


def merge_pins(survivor: Pin, loser: Pin, profile: Profile, resolutions: dict[str, int] | None = None) -> Pin:
    """Merge loser into survivor: reassign every relation, resolve every conflict, delete loser.

    Args:
        survivor: The pin that will remain, absorbing loser's data.
        loser: The pin that will be deleted once its data has moved.
        profile: The profile both pins must belong to.
        resolutions: Maps each :class:`MergeFieldConflict` key (from :func:`plan_merge_conflicts`) to the pin id whose value should be kept.

    Returns:
        The survivor pin (refreshed by the merge).

    Raises:
        ValueError: survivor and loser are the same pin, or either doesn't belong to profile.
        UnresolvedMergeConflictError: a real conflict has no resolution supplied."""
    if survivor.pk == loser.pk:
        raise ValueError("Cannot merge a pin into itself")
    if survivor.profile_id != profile.pk or loser.profile_id != profile.pk:
        raise ValueError("merge_pins requires both pins to belong to the merging profile")

    resolutions = resolutions or {}
    conflicts = plan_merge_conflicts(survivor, loser)
    missing = [conflict.key for conflict in conflicts if conflict.key not in resolutions]
    if missing:
        raise UnresolvedMergeConflictError(missing)

    with transaction.atomic():
        _reparent_children(survivor, loser)
        _merge_aliases(survivor, loser)
        _merge_owners(survivor, loser)
        _merge_reviews(survivor, loser)
        _merge_list_items(survivor, loser)
        _merge_auto_removals(survivor, loser)
        _merge_shares(survivor, loser)
        _merge_custom_field_values(survivor, loser, resolutions)
        _merge_boundaries(survivor, loser, resolutions)
        _merge_article(survivor, loser, resolutions)
        _merge_lineage(survivor, loser)
        survivor.labels.add(*loser.labels.all())

        _merge_albums(survivor, loser)
        # Overlays and custom layers carry no uniqueness constraint on the pin, so they move
        # straight across.
        # All three of these relations CASCADE from Pin and postdate this module - without them the
        # loser's albums, overlays and layers were destroyed by the delete() below.
        MapImageOverlay.objects.filter(parent_pin=loser).update(parent_pin=survivor)
        CustomLayer.objects.filter(parent_pin=loser).update(parent_pin=survivor)
        # Same drift, two more CASCADE relations that postdate this module:
        # an attachment can collide on (image, pin) uniqueness, and a marker's
        # twin is OneToOne, so both get their own dedup-aware helper above.
        _merge_image_attachments(survivor, loser)
        _merge_floorplan_marker(survivor, loser)

        PinVisit.objects.filter(pin=loser).update(pin=survivor)
        Image.objects.filter(pin=loser).update(pin=survivor)
        PinLink.objects.filter(pin=loser).update(pin=survivor)
        PinPropertySale.objects.filter(pin=loser).update(pin=survivor)
        LinkExtraction.objects.filter(pin=loser).update(pin=survivor)
        Comment.objects.filter(pin=loser).update(pin=survivor)
        PinMarkup.objects.filter(parent_pin=loser).update(parent_pin=survivor)
        MarkupMap.objects.filter(pin=loser).update(pin=survivor)
        PinNote.objects.filter(pin=loser).update(pin=survivor)
        TripActivity.objects.filter(pin=loser).update(pin=survivor)
        CustomFieldValue.objects.filter(ref_pin=loser).update(ref_pin=survivor)
        PinSuggestion.objects.filter(pin=loser).update(pin=survivor)
        _repoint_other_merge_suggestions(survivor, loser)

        for markup_map in MarkupMap.objects.filter(inferred_pins=loser):
            markup_map.inferred_pins.remove(loser)
            markup_map.inferred_pins.add(survivor)

        loser.delete()

        # last_visited is a denormalized copy of the newest PinVisit, and the visits above moved
        # across via update(), which fires no signal.
        # Recomputing also saves the survivor, which is what refreshes its cached map payload - the
        # merge has no other invalidation despite the survivor gaining visits, images and labels.
        from urbanlens.dashboard.services.visits.visits import sync_last_visited

        sync_last_visited(survivor)

    survivor.refresh_from_db()
    return survivor
