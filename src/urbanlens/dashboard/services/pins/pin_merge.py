"""Merges two of a profile's own pins into one, consolidating every relation.

Distinct from ``controllers.pin_bulk.PinBulkMergeView`` (the map multi-select
"Merge" button), which only re-parents pins as children - both rows survive,
untouched, just nested. This module is a true consolidating merge: one pin
(the "survivor") absorbs the other's (the "loser's") data, and the loser is
deleted. Used by ``services.pins.pin_merge_suggestions`` to accept a
``PinMergeSuggestion``, but is itself suggestion-agnostic - any future "merge
these two pins" affordance can call :func:`merge_pins` directly.

Every relation FK'd to Pin falls into one of three buckets:

- **Safe bulk reassign** - no uniqueness constraint on the target side, so the
  loser's rows just move onto the survivor (``PinVisit``, ``Image``,
  ``PinLink``, ``PinPropertySale``, ``LinkExtraction``, ``Comment``,
  ``PinMarkup``, ``MarkupMap`` FK + M2M, ``PinNote``, ``TripActivity``,
  ``CustomFieldValue.ref_pin``, ``PinSuggestion``, other ``PinMergeSuggestion``
  rows, the ``labels`` M2M).
- **Auto-dedup** - a uniqueness constraint could conflict, but the conflict is
  unambiguous (identical alias/owner text, the same auto-removal tombstone,
  the same outstanding share, the same list membership) - :func:`merge_pins`
  resolves these itself, keeping one side by a fixed rule and dropping the
  redundant other (``PinAlias``, ``PinOwner``, ``PinAutoRemoval``, ``PinShare``,
  ``PinListItem``; ``Review`` similarly, keeping whichever the same reviewer
  most recently updated).
- **Ask the user** - both pins can hold genuinely different content for the
  same slot (``Article``, a same-type ``Boundary``, a ``CustomFieldValue`` for
  the same ``CustomField``) - see :func:`plan_merge_conflicts`. Silently
  picking one side here would be real, silent data loss, so :func:`merge_pins`
  refuses (:class:`UnresolvedMergeConflictError`) until the caller supplies a
  ``resolutions`` entry naming which pin's value to keep.

Child pins (``Pin.parent_pin``) are re-parented onto the survivor (mirroring
``services.pins.pin_restructure.nest_root_pins``), and ``source_share``/
``inferred_source_share``/``cover_photo`` gap-fill from the loser onto the
survivor when the survivor has none of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.auto_removals.model import PinAutoRemoval
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.custom_fields.model import CustomFieldValue
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
    from django.db.models import Model

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class UnresolvedMergeConflictError(ValueError):
    """Raised when merge_pins is called without a resolution for a real conflict.

    Attributes:
        keys: The MergeFieldConflict.key values that had no resolution supplied.
    """

    def __init__(self, keys: list[str]) -> None:
        """Store the unresolved keys and build a descriptive message.

        Args:
            keys: The conflict keys missing a resolution.
        """
        self.keys = keys
        super().__init__(f"Unresolved merge conflicts: {', '.join(keys)}")


class PinMergeCollisionError(ValueError):
    """Raised when a merge cannot proceed without destroying data.

    Two situations produce this, both because leaving a pin parented to
    ``loser`` would let ``Pin.parent_pin``'s CASCADE take it - and anything
    nested beneath it, survivor included - out with ``loser.delete()``:

    - A child pin has to be detached to top level (because re-parenting it
      under the survivor would close a loop), but another top-level pin
      already occupies its location.
    - The survivor is itself one of loser's direct children and has to move
      to loser's own parent, but another top-level pin already occupies the
      survivor's location (only possible when that parent is None, i.e. the
      survivor would become a new top-level pin).

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        """Store the caller-safe message.

        Args:
            message: Explanation of which pin blocked the merge.
        """
        self.safe_message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MergeFieldConflict:
    """One place two pins hold genuinely different data for the same slot.

    Only the three relations where both sides can hold distinct, real content
    for one slot ever produce one of these - see the module docstring's
    "ask the user" bucket. ``key`` is the stable id a caller passes back in
    ``merge_pins``'s ``resolutions`` dict to say which pin's value to keep.
    """

    key: str
    label: str
    pin_a_summary: str
    pin_b_summary: str


def _get_article(pin: Pin) -> Article | None:
    """The pin's Article, or None - safe against the reverse OneToOne's DoesNotExist."""
    return getattr(pin, "article", None)


def plan_merge_conflicts(pin_a: Pin, pin_b: Pin) -> list[MergeFieldConflict]:
    """Every field where pin_a and pin_b both hold real, possibly-divergent data.

    Args:
        pin_a: One pin under consideration.
        pin_b: The other pin under consideration.

    Returns:
        Conflicts the accepting user must resolve before ``merge_pins`` will
        merge these two pins - empty when nothing needs a decision.
    """
    conflicts: list[MergeFieldConflict] = []

    article_a, article_b = _get_article(pin_a), _get_article(pin_b)
    if article_a is not None and article_b is not None:
        conflicts.append(
            MergeFieldConflict(
                key="article",
                label="Both pins have an article",
                pin_a_summary=f"{article_a.word_count()} words, updated {article_a.updated.date().isoformat()}",
                pin_b_summary=f"{article_b.word_count()} words, updated {article_b.updated.date().isoformat()}",
            ),
        )

    boundaries_a = {boundary.boundary_type: boundary for boundary in Boundary.objects.filter(pin=pin_a)}
    boundaries_b = {boundary.boundary_type: boundary for boundary in Boundary.objects.filter(pin=pin_b)}
    for boundary_type in sorted(set(boundaries_a) & set(boundaries_b)):
        conflicts.append(
            MergeFieldConflict(
                key=f"boundary:{boundary_type}",
                label=f"Both pins have a {boundary_type} boundary",
                pin_a_summary=f"Updated {boundaries_a[boundary_type].updated.date().isoformat()}",
                pin_b_summary=f"Updated {boundaries_b[boundary_type].updated.date().isoformat()}",
            ),
        )

    values_a = {value.field_id: value for value in CustomFieldValue.objects.filter(pin=pin_a).select_related("field")}
    values_b = {value.field_id: value for value in CustomFieldValue.objects.filter(pin=pin_b).select_related("field")}
    for field_id in sorted(set(values_a) & set(values_b)):
        value_a, value_b = values_a[field_id], values_b[field_id]
        conflicts.append(
            MergeFieldConflict(
                key=f"custom_field:{field_id}",
                label=f'Both pins have a value for "{value_a.field.name}"',
                pin_a_summary=value_a.display_value or "(empty)",
                pin_b_summary=value_b.display_value or "(empty)",
            ),
        )

    return conflicts


def _save_within_savepoint(instance: Model, update_fields: list[str]) -> bool:
    """Save a row that may collide with a uniqueness constraint, recoverably.

    Every reassignment in this module runs inside ``merge_pins``' single
    ``transaction.atomic()`` block. Postgres aborts the *whole* transaction on
    a failed statement, so catching ``IntegrityError`` there and carrying on
    makes the next query raise ``TransactionManagementError`` instead - which
    turned each of this module's "drop the duplicate and continue" recoveries
    into a merge that failed outright. The nested ``atomic()`` is a savepoint,
    so only the failed statement rolls back and the caller's recovery can run.

    Args:
        instance: The model instance being reassigned onto the survivor.
        update_fields: Fields to write, passed straight to ``save()``.

    Returns:
        True when the row was written; False when it collided and the caller
        should apply its own dedup rule.
    """
    try:
        with transaction.atomic():
            instance.save(update_fields=update_fields)
    except IntegrityError:
        return False
    return True


def _reparent_children(survivor: Pin, loser: Pin) -> None:
    """Re-parent loser's child pins onto survivor, skipping any that would create a cycle.

    Mirrors ``services.pins.pin_restructure.nest_root_pins`` for the normal case. A
    child that would create a cycle (survivor sits somewhere beneath that
    child already) is detached to root instead of just left alone - leaving it
    pointed at ``loser`` would let ``loser.delete()``'s ``CASCADE`` on
    ``parent_pin`` destroy that child, and everything nested beneath it,
    survivor included.

    When survivor is itself one of loser's direct children, it is re-pointed
    at loser's own parent instead of being left alone - the same CASCADE would
    otherwise take survivor down with the loser it is supposed to absorb.
    """
    for child in list(loser.detail_pins.all()):
        if child.pk == survivor.pk:
            survivor.parent_pin = loser.parent_pin
            if not _save_within_savepoint(survivor, ["parent_pin", "updated"]):
                raise PinMergeCollisionError(
                    f"Cannot merge: survivor pin {survivor.pk} has to move to the loser's own parent to avoid being deleted with it, but another top-level pin already occupies its location.",
                )
            continue
        if child.would_create_cycle(survivor):
            logger.warning("Pin merge: detaching child pin %s to root - re-parenting under the survivor would create a cycle.", child.pk)
            child.parent_pin = None
            if not _save_within_savepoint(child, ["parent_pin", "updated"]):
                # It stays parented to the pin about to be deleted, and
                # Pin.parent_pin CASCADEs - so this child, and the survivor
                # somewhere beneath it, would both be destroyed by the delete
                # this detach exists to prevent. Refuse the merge instead.
                raise PinMergeCollisionError(
                    f"Cannot merge: child pin {child.pk} has to be detached to top level to avoid a loop, but another top-level pin already occupies its location.",
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
    """Repoint any OTHER pending suggestion mentioning loser onto survivor.

    A suggestion naming exactly this (survivor, loser) pair is left alone -
    the caller (``services.pins.pin_merge_suggestions.accept_pin_merge_suggestion``)
    marks that one accepted itself, and repointing it here would leave both
    its pin_a and pin_b pointing at the same pin, violating
    ``db_pin_merge_suggestion_distinct_pins``.
    """
    for suggestion in PinMergeSuggestion.objects.for_pin(loser):
        if {suggestion.pin_a_id, suggestion.pin_b_id} == {survivor.pk, loser.pk}:
            continue
        if suggestion.pin_a_id == loser.pk:
            suggestion.pin_a = survivor
            suggestion.save(update_fields=["pin_a", "updated"])
        else:
            suggestion.pin_b = survivor
            suggestion.save(update_fields=["pin_b", "updated"])


def _merge_albums(survivor: Pin, loser: Pin) -> None:
    """Move the loser's albums onto the survivor, re-slugging on collision.

    ``uq_album_pin_slug`` is unique on ``(parent_pin, slug)``, so a plain
    reassign fails when both pins have an album with the same slug - two pins
    each carrying a "Photos" album is the ordinary case, and both hold real
    images, so neither may be dropped. Clearing the slug lets the model's
    ``save()`` mint a fresh unique one (it only generates when the slug is
    empty), keeping both albums.

    Args:
        survivor: The pin absorbing the albums.
        loser: The pin being merged away.
    """
    taken = set(Album.objects.filter(parent_pin=survivor).values_list("slug", flat=True))
    for album in Album.objects.filter(parent_pin=loser):
        album.parent_pin = survivor
        if album.slug in taken:
            album.slug = ""
        album.save()
        taken.add(album.slug)


def merge_pins(survivor: Pin, loser: Pin, profile: Profile, resolutions: dict[str, int] | None = None) -> Pin:
    """Merge loser into survivor: reassign every relation, resolve every conflict, delete loser.

    See the module docstring for the full per-relation rule table.

    Args:
        survivor: The pin that will remain, absorbing loser's data.
        loser: The pin that will be deleted once its data has moved.
        profile: The profile both pins must belong to.
        resolutions: Maps each :class:`MergeFieldConflict` key (from
            :func:`plan_merge_conflicts`) to the pin id whose value should be
            kept. Every key returned by ``plan_merge_conflicts(survivor, loser)``
            at call time must be present, or this raises
            :class:`UnresolvedMergeConflictError` - the caller should recompute
            the conflict list fresh and re-prompt rather than guess, since pin
            state may have changed since the form was rendered.

    Returns:
        The survivor pin (refreshed by the merge).

    Raises:
        ValueError: survivor and loser are the same pin, or either doesn't
            belong to profile.
        UnresolvedMergeConflictError: a real conflict has no resolution supplied.
    """
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
        # Overlays and custom layers carry no uniqueness constraint on the pin,
        # so they move straight across. All three of these relations CASCADE
        # from Pin and postdate this module - without them the loser's albums,
        # overlays and layers were destroyed by the delete() below.
        MapImageOverlay.objects.filter(parent_pin=loser).update(parent_pin=survivor)
        CustomLayer.objects.filter(parent_pin=loser).update(parent_pin=survivor)

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

        # last_visited is a denormalized copy of the newest PinVisit, and the visits
        # above moved across via update(), which fires no signal. Recomputing also
        # saves the survivor, which is what refreshes its cached map payload - the
        # merge has no other invalidation despite the survivor gaining visits,
        # images and labels.
        from urbanlens.dashboard.services.visits.visits import sync_last_visited

        sync_last_visited(survivor)

    survivor.refresh_from_db()
    return survivor
