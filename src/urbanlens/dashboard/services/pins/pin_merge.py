"""
Merges two of a profile's own pins into one, consolidating every relation without losing what either held.
Distinct from ``controllers.pin_bulk.PinBulkMergeView`` (the map multi-select "Merge" button), which only re-parents pins as children - both rows survive, untouched, just nested.

Only the loser row is deleted. Everything it held either moves onto the survivor, folds into the survivor's equivalent row, or - where the survivor has exactly one slot and keeps its own value - is written down:

- Article: the kept article stays live and the other's whole revision history moves onto it (``services.wiki.article_merge``).
- Boundary: the outline not kept becomes a polygon on the survivor's own Markup layer.
- Custom field values, and every pin column the survivor keeps its own value for: listed in one private ``PinNote`` on the survivor.
- Duplicate rows (an owner, a pending share, the day's automatic visit): folded into the survivor's twin, with every row that referenced the duplicate repointed first.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import UTC
import json
import logging
from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, transaction
from django.db.models.fields.files import FieldFile
from django.utils import timezone
from django.utils.text import capfirst

from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.auto_removals.model import PinAutoRemoval
from urbanlens.dashboard.models.boundary.model import Boundary
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.custom_fields.model import CustomFieldValue
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.images.attachment import ImageAttachment
from urbanlens.dashboard.models.images.issues import PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.link_extraction.model import LinkExtraction
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.meta import MarkupType
from urbanlens.dashboard.models.markup.model import CustomLayer, MarkupMap, PinMarkup
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_import_failures.model import PinImportFailure
from urbanlens.dashboard.models.pin_list.model import PinListItem
from urbanlens.dashboard.models.pin_merge_suggestions.model import PinMergeSuggestion
from urbanlens.dashboard.models.pin_share.meta import PinShareOrigin, PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion
from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.trips.model import TripActivity
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.core.row_merge import column_field, repoint_references
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_NOTE_LENGTH

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from django.db.models import Model

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.floorplans.model import FloorplanMarker
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


class UnresolvedMergeConflictError(ValueError):
    """
    Raised when merge_pins is called without a resolution for a real conflict.

    Attributes:
        keys: The MergeFieldConflict.key values that had no resolution supplied.
    """

    def __init__(self, keys: list[str]) -> None:
        """
        Store the unresolved keys and build a descriptive message.

        Args:
            keys: The conflict keys missing a resolution.
        """
        self.keys = keys
        super().__init__(f"Unresolved merge conflicts: {', '.join(keys)}")


class PinMergeCollisionError(ValueError):
    """Raised when a merge cannot proceed without destroying data."""


class SurvivorRelocationCollisionError(PinMergeCollisionError):
    """
    The survivor is one of loser's direct children and must move to loser's own parent - avoiding being deleted with it - but another top-level pin already occupies that location.
    Only possible when loser's parent is None, i.e. the survivor would become a new top-level pin."""


class ChildDetachCollisionError(PinMergeCollisionError):
    """A child of loser has to be detached to top level - re-parenting it under the survivor would close a loop - but another top-level pin already occupies its location."""


@dataclass(frozen=True, slots=True)
class MergeFieldConflict:
    """
    One place two pins hold genuinely different data for the same slot.
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
    """
    Everything :func:`plan_merge_conflicts` compares, for many pins at once.
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
        if boundary.pin_id is not None:
            boundaries[boundary.pin_id][boundary.boundary_type] = boundary
    values: collections.defaultdict[int, dict[int, CustomFieldValue]] = collections.defaultdict(dict)
    for value in CustomFieldValue.objects.filter(pin_id__in=pin_ids).select_related("field"):
        if value.pin_id is not None:
            values[value.pin_id][value.field_id] = value

    return {pin_id: _PinConflictData(articles.get(pin_id), boundaries[pin_id], values[pin_id]) for pin_id in pin_ids}


def _conflicts_between(pin_a: Pin, pin_b: Pin, data: dict[int, _PinConflictData]) -> list[MergeFieldConflict]:
    """
    The comparison half of :func:`plan_merge_conflicts`, over fetched data.

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
    """
    Every field where pin_a and pin_b both hold real, possibly-divergent data.

    Args:
        pin_a: One pin under consideration.
        pin_b: The other pin under consideration.

    Returns:
        Conflicts the accepting user must resolve before ``merge_pins`` will merge these two pins - empty when nothing needs a decision."""
    return _conflicts_between(pin_a, pin_b, _conflict_data([pin_a, pin_b]))


def plan_merge_conflicts_bulk(pairs: Sequence[tuple[Pin, Pin]]) -> dict[tuple[int, int], list[MergeFieldConflict]]:
    """
    Conflicts for many pairs, in three queries for the whole batch.
    A caller that then *merges* must re-plan per pair with :func:`plan_merge_conflicts`, because each merge changes what the next one collides with.

    Args:
        pairs: ``(pin_a, pin_b)`` pairs, in any order.

    Returns:
        Conflicts keyed by ``(pin_a.pk, pin_b.pk)``."""
    data = _conflict_data([pin for pair in pairs for pin in pair])
    return {(pin_a.pk, pin_b.pk): _conflicts_between(pin_a, pin_b, data) for pin_a, pin_b in pairs}


def _save_within_savepoint(instance: Model, update_fields: list[str]) -> bool:
    """
    Save a row that may collide with a uniqueness constraint, recoverably.

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


@dataclass(slots=True)
class _MergeRecord:
    """
    What the survivor kept its own value for, written to it as one private note once the merge is done.

    Attributes:
        lines: ``(label, value)`` pairs, in the order they were found.
    """

    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, label: str, value: str) -> None:
        """
        Record one value the survivor did not keep.

        Args:
            label: What the value was, e.g. ``"Priority"``.
            value: The value as the user would read it.
        """
        self.lines.append((label, value))


def _pin_label(pin: Pin) -> str:
    """A name for a pin that is safe to compute while the merge is rewriting it."""
    return pin.effective_name or f"Pin {pin.pk}"


def _join_text(kept: str | None, extra: str | None) -> str | None:
    """
    Append *extra* to *kept* as its own paragraph, unless it adds nothing.

    Args:
        kept: The text that stays first.
        extra: Text from the row being folded in.

    Returns:
        The combined text.
    """
    kept_text, extra_text = (kept or "").strip(), (extra or "").strip()
    if not extra_text or extra_text == kept_text or extra_text in kept_text:
        return kept
    return f"{kept_text}\n\n{extra_text}" if kept_text else extra


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


#: Owner columns gap-filled from a folded duplicate; ``notes`` is joined instead, and a value that
#: differs from the kept owner's is written into those notes.
_OWNER_DETAIL_FIELDS = ("company_name", "address", "care_of", "phone", "email")


def _fold_owner(duplicate: PinOwner, kept: PinOwner) -> None:
    """
    Fold *duplicate* - the same owner name, differently cased - into *kept*, then delete it.
    The name is the only thing the two are known to share: the duplicate's contact details fill the kept row's blanks, any that differ are written into its notes, and every sale that listed the duplicate lists the kept row instead.

    Args:
        duplicate: The loser's owner row.
        kept: The survivor's owner with the same name.
    """
    differing: list[str] = []
    for name in _OWNER_DETAIL_FIELDS:
        theirs, ours = getattr(duplicate, name), getattr(kept, name)
        if not theirs or theirs == ours:
            continue
        if ours:
            differing.append(f"{capfirst(column_field(PinOwner, name).verbose_name)}: {theirs}")
        else:
            setattr(kept, name, theirs)
    kept.notes = _join_text(_join_text(kept.notes, duplicate.notes), "\n".join(differing)) or ""
    kept.save()
    repoint_references(duplicate, kept)
    duplicate.delete()


def _merge_owners(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's owners onto survivor, folding a case-insensitive duplicate into the survivor's owner of that name."""
    survivor_owners = {owner.name.casefold(): owner for owner in survivor.owners.all()}
    for owner in list(loser.owners.all()):
        kept = survivor_owners.get(owner.name.casefold())
        if kept is not None:
            _fold_owner(owner, kept)
            continue
        owner.pin = survivor
        if _save_within_savepoint(owner, ["pin", "updated"]):
            survivor_owners[owner.name.casefold()] = owner
        else:
            kept = PinOwner.objects.filter(pin=survivor, name__iexact=owner.name).first()
            if kept is not None:
                _fold_owner(owner, kept)


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


def _fold_share(duplicate: PinShare, kept: PinShare) -> None:
    """
    Fold a duplicate share of the loser into the survivor's share to the same recipient, then delete it.
    Only one pending (or one map-detected) share per pin and recipient may exist, but the duplicate can carry its own message, photos and map, and other rows hang off it - reshares, bundled shares (which CASCADE), direct-message links, exposure rows - so all of that moves to the kept share rather than going with the delete.

    Args:
        duplicate: The loser's share.
        kept: The survivor's share to the same recipient.
    """
    kept.message = _join_text(kept.message, duplicate.message)
    if not kept.shared_name and duplicate.shared_name:
        kept.shared_name = duplicate.shared_name
    for column in ("markup_map_id", "detected_via_map_id", "detected_via_message_id"):
        if getattr(kept, column) is None and getattr(duplicate, column) is not None:
            setattr(kept, column, getattr(duplicate, column))
    kept.save()
    kept.images.add(*duplicate.images.all())
    repoint_references(duplicate, kept)
    duplicate.delete()


def _merge_shares(survivor: Pin, loser: Pin) -> None:
    """Reassign loser's shares onto survivor, folding a duplicate pending/detected share to the same recipient into the survivor's."""
    survivor_pending = {share.to_profile_id: share for share in PinShare.objects.filter(pin=survivor, status=PinShareStatus.PENDING)}
    survivor_detected = {share.to_profile_id: share for share in PinShare.objects.filter(pin=survivor, origin=PinShareOrigin.MAP_DETECTED)}
    for share in list(PinShare.objects.filter(pin=loser)):
        twin = None
        if share.status == PinShareStatus.PENDING:
            twin = survivor_pending.get(share.to_profile_id)
        if twin is None and share.origin == PinShareOrigin.MAP_DETECTED:
            twin = survivor_detected.get(share.to_profile_id)
        if twin is not None:
            _fold_share(share, twin)
            continue
        share.pin = survivor
        if not _save_within_savepoint(share, ["pin", "updated"]):
            # Collided with a share the maps above did not hold (written concurrently); leave it to
            # the loser's delete, exactly as before rather than guessing at a twin.
            logger.warning("Pin merge: share %s could not move to pin %s and goes with the loser.", share.pk, survivor.pk)
            continue
        if share.status == PinShareStatus.PENDING:
            survivor_pending[share.to_profile_id] = share
        if share.origin == PinShareOrigin.MAP_DETECTED:
            survivor_detected[share.to_profile_id] = share


def _merge_custom_field_values(survivor: Pin, loser: Pin, resolutions: dict[str, int], record: _MergeRecord, labels: dict[int, str]) -> None:
    """
    Reassign loser's custom field values onto survivor, applying the user's choice for any conflicting field.
    A pin holds one value per field, so the value not chosen is written into the merge note.

    Args:
        survivor: The pin that stays.
        loser: The pin being merged away.
        resolutions: The user's per-conflict choices.
        record: Collects the values not kept.
        labels: Each pin's display name, by pk, as it was before the merge.
    """
    survivor_values = {value.field_id: value for value in CustomFieldValue.objects.filter(pin=survivor).select_related("field")}
    for value in list(CustomFieldValue.objects.filter(pin=loser).select_related("field")):
        existing = survivor_values.get(value.field_id)
        if existing is None:
            value.pin = survivor
            value.save(update_fields=["pin", "updated"])
            survivor_values[value.field_id] = value
            continue
        if resolutions.get(f"custom_field:{value.field_id}") == loser.pk:
            kept, dropped, dropped_from = value, existing, survivor
        else:
            kept, dropped, dropped_from = existing, value, loser
        if dropped.display_value != kept.display_value:
            record.add(f"{value.field.name} ({labels[dropped_from.pk]})", dropped.display_value or "(empty)")
        dropped.delete()
        if kept is value:
            value.pin = survivor
            value.save(update_fields=["pin", "updated"])


def _keep_outline(survivor: Pin, boundary: Boundary, profile: Profile, source_label: str) -> int:
    """
    Copy a boundary the survivor cannot hold onto its Markup layer, one polygon per part.
    A pin holds one boundary per type, and pin boundaries have no revision history (``BoundaryRevision`` is wiki-only), so the outline not kept would otherwise be deleted. The Markup layer is the pin's own private geometry store: the outline stays visible on the pin's map and the user can redraw the boundary from it.
    Only a drawn outline is kept - a pin's generated outline is fitted from its child pins, which all move to the survivor, so it is regenerated rather than lost.

    Args:
        survivor: The pin that stays.
        boundary: The boundary being dropped.
        profile: The pin owner.
        source_label: The name of the pin the boundary belonged to.

    Returns:
        How many markup polygons were written.
    """
    outline = boundary.polygon or (None if boundary.generated_from_children else boundary.generated_polygon)
    if outline is None:
        return 0
    label = f"{boundary.get_boundary_type_display()} boundary from {source_label}"
    for part in outline:
        PinMarkup.objects.create(
            parent_pin=survivor,
            profile=profile,
            markup_type=MarkupType.POLYGON,
            geometry=json.loads(part.json),
            label=label[:500],
            fill_opacity=15,
        )
    return len(outline)


def _merge_boundaries(survivor: Pin, loser: Pin, resolutions: dict[str, int], profile: Profile, record: _MergeRecord, labels: dict[int, str]) -> None:
    """
    Reassign loser's boundaries onto survivor, applying the user's choice for any conflicting boundary type.
    The outline not chosen is kept as markup on the survivor - see :func:`_keep_outline`.

    Args:
        survivor: The pin that stays.
        loser: The pin being merged away.
        resolutions: The user's per-conflict choices.
        profile: The pin owner.
        record: Collects what was not kept.
        labels: Each pin's display name, by pk, as it was before the merge.
    """
    survivor_types = {boundary.boundary_type: boundary for boundary in Boundary.objects.filter(pin=survivor)}
    for boundary in list(Boundary.objects.filter(pin=loser)):
        existing = survivor_types.get(boundary.boundary_type)
        if existing is None:
            boundary.pin = survivor
            boundary.save(update_fields=["pin", "updated"])
            survivor_types[boundary.boundary_type] = boundary
            continue
        if resolutions.get(f"boundary:{boundary.boundary_type}") == loser.pk:
            kept, dropped, dropped_from = boundary, existing, survivor
        else:
            kept, dropped, dropped_from = existing, boundary, loser
        if _keep_outline(survivor, dropped, profile, labels[dropped_from.pk]):
            record.add(f"{dropped.get_boundary_type_display()} boundary ({labels[dropped_from.pk]})", "kept as an outline on this pin's Markup layer")
        dropped.delete()
        if kept is boundary:
            boundary.pin = survivor
            boundary.save(update_fields=["pin", "updated"])
            survivor_types[boundary.boundary_type] = boundary


def _merge_article(survivor: Pin, loser: Pin, resolutions: dict[str, int], profile: Profile, record: _MergeRecord, labels: dict[int, str]) -> None:
    """
    Keep one pin's Article live when both have one (per resolutions["article"]), and the other's whole history behind it.
    The article not chosen stops being live text but none of it is deleted: every one of its revisions moves onto the kept article, where the history page can show, diff and restore it.

    Args:
        survivor: The pin that stays.
        loser: The pin being merged away.
        resolutions: The user's per-conflict choices.
        profile: The pin owner, credited with the merge revision.
        record: Collects what was not kept.
        labels: Each pin's display name, by pk, as it was before the merge.
    """
    from urbanlens.dashboard.services.wiki import article_merge

    loser_article = _get_article(loser)
    if loser_article is None:
        return
    survivor_article = _get_article(survivor)
    if survivor_article is None:
        loser_article.pin = survivor
        loser_article.save(update_fields=["pin", "updated"])
        return
    if resolutions.get("article") == loser.pk:
        kept, absorbed, absorbed_from = loser_article, survivor_article, survivor
    else:
        kept, absorbed, absorbed_from = survivor_article, loser_article, loser
    article_merge.record_live_text(kept)
    article_merge.adopt_history(kept, absorbed)
    # The OneToOne frees the survivor only once the absorbed row is gone.
    absorbed.delete()
    if kept.pin_id != survivor.pk:
        kept.pin = survivor
        kept.save(update_fields=["pin", "updated"])
    article_merge.write_merge_revision(
        kept,
        content=kept.content,
        editor=profile,
        edit_summary=f"Merged pins: kept this text; {labels[absorbed_from.pk]}'s article is in the history below",
    )
    record.add(f"Article ({labels[absorbed_from.pk]})", "kept in this pin's article history")


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
    """
    Move the loser's albums onto the survivor, re-slugging on collision.
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


# How :func:`_merge_scalars` treats each of the loser's own columns.
#: The survivor's identity or position; the loser's goes with it (its slug and uuid are tombstoned).
_IDENTITY = "identity"
#: Recomputed from the merged relations afterwards.
_DERIVED = "derived"
#: Share lineage and cover photo, gap-filled by :func:`_merge_lineage`.
_LINEAGE = "lineage"
#: The survivor's value when it has one, else the loser's; a different loser value goes into the merge note.
_GAP_FILL = "gap_fill"
#: The larger of the two, the other going into the note: a higher danger is the cautious reading, and a
#: higher priority or vulnerability is what the user last thought of one copy of this place.
_HIGHER = "higher"
#: True when either pin says so - a declined restructure offer is a permanent "no".
_EITHER = "either"
#: True only when both say so - the visits that queue asks about now include the loser's.
_BOTH = "both"
#: The later of two timestamps.
_LATER = "later"
#: The union of two lists.
_UNION = "union"
#: Travels with the column it qualifies (see ``_COMPANIONS``).
_COMPANION = "companion"

#: Every concrete Pin column, and what a merge does with the loser's value.
#: Test-enforced complete, so a column added later fails until somebody decides rather than being
#: silently deleted with the loser.
PIN_FIELD_POLICY: dict[str, str] = {
    "id": _IDENTITY,
    "created": _IDENTITY,
    "updated": _IDENTITY,
    "uuid": _IDENTITY,
    "slug": _IDENTITY,
    "profile": _IDENTITY,
    "location": _IDENTITY,
    "parent_pin": _IDENTITY,
    "last_visited": _DERIVED,
    "source_share": _LINEAGE,
    "inferred_source_share": _LINEAGE,
    "cover_photo": _LINEAGE,
    "fences": _GAP_FILL,
    "alarms": _GAP_FILL,
    "cameras": _GAP_FILL,
    "security": _GAP_FILL,
    "signs": _GAP_FILL,
    "vps": _GAP_FILL,
    "plywood": _GAP_FILL,
    "locked": _GAP_FILL,
    "name": _GAP_FILL,
    "name_is_user_provided": _COMPANION,
    "icon": _GAP_FILL,
    "description": _GAP_FILL,
    "custom_icon": _GAP_FILL,
    "custom_icon_upload": _COMPANION,
    "pin_type": _GAP_FILL,
    "pin_type_is_user_provided": _COMPANION,
    "indoor_outdoor": _GAP_FILL,
    "color": _GAP_FILL,
    "detail_bg_color": _GAP_FILL,
    "detail_bg_opacity": _COMPANION,
    "detail_border_color": _GAP_FILL,
    "detail_border_opacity": _COMPANION,
    "date_built": _GAP_FILL,
    "date_abandoned": _GAP_FILL,
    "date_last_active": _GAP_FILL,
    "wiki": _GAP_FILL,
    "priority": _HIGHER,
    "vulnerability": _HIGHER,
    "danger": _HIGHER,
    "restructure_offer_dismissed": _EITHER,
    "unlogged_visit_dismissed": _BOTH,
    "last_viewed_at": _LATER,
    "buildings_auto_nested_at": _LATER,
    "auto_nested_buildings": _UNION,
}

#: For the policies that pick one side outright: whether the loser's value replaces the survivor's.
_LOSER_REPLACES: dict[str, Callable[[Any, Any], bool]] = {
    _EITHER: lambda ours, theirs: bool(theirs) and not ours,
    _BOTH: lambda ours, theirs: bool(ours) and not theirs,
    _LATER: lambda ours, theirs: theirs is not None and (ours is None or theirs > ours),
}

#: Columns that only mean something next to another one, so they move when it moves.
_COMPANIONS: dict[str, tuple[str, ...]] = {
    "name": ("name_is_user_provided",),
    "pin_type": ("pin_type_is_user_provided",),
    "custom_icon": ("custom_icon_upload",),
    "detail_bg_color": ("detail_bg_opacity",),
    "detail_border_color": ("detail_border_opacity",),
}

#: The flag marking a column's value as the user's own choice rather than an import's or a
#: classifier's. A chosen value beats an automatic one, whichever pin holds it.
_USER_CHOSEN: dict[str, str] = {"name": "name_is_user_provided", "pin_type": "pin_type_is_user_provided"}

#: Gap-filled columns that are not worth a line in the note when the survivor keeps its own: the
#: loser's name is kept as an alias instead.
_NOT_NOTED = frozenset({"name"})


def _has_value(pin: Pin, name: str) -> bool:
    """
    Whether *pin* holds a real value for column *name*, as opposed to an empty or default one.

    Args:
        pin: Either pin.
        name: A ``_GAP_FILL`` column.

    Returns:
        True when the value is something a person or a pipeline set.
    """
    model_field = column_field(pin, name)
    value = getattr(pin, model_field.attname)
    if name == "custom_icon":
        return bool(pin.custom_icon) or bool(pin.custom_icon_upload)
    if name in _USER_CHOSEN and getattr(pin, _USER_CHOSEN[name]):
        return True
    if value is None or value == "":
        return False
    return not (model_field.has_default() and not callable(model_field.default) and value == model_field.default)


def _display(pin: Pin, name: str) -> str:
    """
    One column's value as the merge note shows it.

    Args:
        pin: The pin holding the value.
        name: The column.

    Returns:
        Readable text.
    """
    model_field = column_field(pin, name)
    value = getattr(pin, model_field.name)
    if model_field.choices:
        return str(dict(model_field.flatchoices).get(value, value))
    if isinstance(value, FieldFile):
        return value.name or pin.custom_icon_upload
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _merge_scalars(survivor: Pin, loser: Pin, record: _MergeRecord, labels: dict[int, str]) -> None:
    """
    Merge the loser's own columns into the survivor's per ``PIN_FIELD_POLICY``, noting every value not kept.

    Args:
        survivor: The pin that stays.
        loser: The pin being merged away.
        record: Collects the values not kept.
        labels: Each pin's display name, by pk, as it was before the merge.
    """
    from urbanlens.dashboard.models.aliases.model import PinAlias
    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    changed: list[str] = []

    def take(name: str) -> None:
        for column in (name, *_COMPANIONS.get(name, ())):
            attname = column_field(survivor, column).attname
            value = getattr(loser, attname)
            # A file is taken by its stored name; the loser's FieldFile is bound to the loser.
            setattr(survivor, attname, value.name if isinstance(value, FieldFile) else value)
            changed.append(column)

    for name, policy in PIN_FIELD_POLICY.items():
        if policy == _GAP_FILL:
            ours, theirs = _has_value(survivor, name), _has_value(loser, name)
            if not theirs:
                continue
            chosen_flag = _USER_CHOSEN.get(name)
            loser_wins = not ours or (chosen_flag is not None and getattr(loser, chosen_flag) and not getattr(survivor, chosen_flag))
            dropped_from = survivor if loser_wins else loser
            if ours and _display(survivor, name) != _display(loser, name) and name not in _NOT_NOTED:
                record.add(f"{capfirst(column_field(survivor, name).verbose_name)} ({labels[dropped_from.pk]})", _display(dropped_from, name))
            if loser_wins:
                take(name)
        elif policy == _HIGHER:
            ours, theirs = getattr(survivor, name), getattr(loser, name)
            if ours != theirs:
                lower_from = survivor if theirs > ours else loser
                record.add(f"{capfirst(column_field(survivor, name).verbose_name)} ({labels[lower_from.pk]})", str(getattr(lower_from, name)))
                if theirs > ours:
                    take(name)
        elif policy in _LOSER_REPLACES:
            if _LOSER_REPLACES[policy](getattr(survivor, name), getattr(loser, name)):
                take(name)
        elif policy == _UNION and getattr(loser, name):
            merged = list(getattr(survivor, name) or [])
            seen = {json.dumps(item, sort_keys=True, default=str) for item in merged}
            for item in getattr(loser, name):
                if json.dumps(item, sort_keys=True, default=str) not in seen:
                    merged.append(item)
            if len(merged) != len(getattr(survivor, name) or []):
                setattr(survivor, name, merged)
                changed.append(name)

    if changed:
        survivor.save(update_fields=[*dict.fromkeys(changed), "updated"])
    # The name the survivor did not take stays findable: aliases are every name a pin has had.
    if loser.name and is_meaningful_name(loser.name) and (loser.name or "").casefold() != (survivor.name or "").casefold():
        PinAlias.objects.resolve_or_create(survivor, loser.name.strip())


def _fold_visit(duplicate: PinVisit, kept: PinVisit) -> None:
    """
    Fold the loser's automatic visit into the survivor's one for the same UTC day, then delete it.
    ``db_pv_one_geo_per_pin_day`` allows one geolocation visit per pin per day, so moving the loser's alongside the survivor's crashed the whole merge. They describe the same day at the same place, so they become one visit: the earlier arrival, both sets of notes and participants, whichever map and route exist, and every row that pointed at the duplicate (its photos, suggestions, external participants).
    A second map the kept visit has no room for stays the owner's, associated with the survivor pin.

    Args:
        duplicate: The loser's visit.
        kept: The survivor's visit on the same day.
    """
    kept.visited_at = min(kept.visited_at, duplicate.visited_at)
    kept.notes = _join_text(kept.notes, duplicate.notes)
    kept.tentative = kept.tentative and duplicate.tentative
    if kept.route_id is None:
        kept.route_id = duplicate.route_id
    if kept.markup_map_id is None:
        kept.markup_map_id = duplicate.markup_map_id
    elif duplicate.markup_map_id is not None:
        MarkupMap.objects.filter(pk=duplicate.markup_map_id, pin__isnull=True).update(pin=kept.pin_id)
    kept.save()
    kept.participants.add(*duplicate.participants.all())
    repoint_references(duplicate, kept)
    duplicate.delete()


def _merge_visits(survivor: Pin, loser: Pin) -> None:
    """Move loser's visits onto survivor, folding an automatic visit that would collide with the survivor's for that day."""
    kept_by_day = {visit.visited_at.astimezone(UTC).date(): visit for visit in PinVisit.objects.filter(pin=survivor, source=VisitSource.GEOLOCATION)}
    for visit in PinVisit.objects.filter(pin=loser, source=VisitSource.GEOLOCATION).order_by("visited_at"):
        day = visit.visited_at.astimezone(UTC).date()
        kept = kept_by_day.get(day)
        if kept is None:
            visit.pin = survivor
            visit.save(update_fields=["pin", "updated"])
            kept_by_day[day] = visit
        else:
            _fold_visit(visit, kept)
    PinVisit.objects.filter(pin=loser).update(pin=survivor)


def _note_texts(header: str, body: str) -> list[str]:
    """
    Split a merge note into texts that each fit a ``PinNote``, without losing a character.
    Each text starts with the header (``(continued)`` after the first). A break falls on a paragraph boundary when one
    is in the second half of the room left, else mid-text - a single value longer than a note (a full description)
    simply continues in the next one.

    Args:
        header: The note's first line.
        body: Everything after it.

    Returns:
        The note texts, in order.
    """
    texts: list[str] = []
    current_header, remaining = header, body
    while remaining:
        room = MAX_PIN_NOTE_LENGTH - len(current_header) - 2
        piece, remaining = remaining[:room], remaining[room:]
        if remaining and (cut := piece.rfind("\n\n")) > room // 2:
            piece, remaining = piece[:cut], piece[cut + 2 :] + remaining
        texts.append(f"{current_header}\n\n{piece}")
        current_header = f"{header} (continued)"
    return texts


def _write_merge_note(survivor: Pin, record: _MergeRecord, labels: dict[int, str], loser_pk: int) -> None:
    """
    Write what the survivor did not keep as one private note on it, continued across notes only when it exceeds one.

    Args:
        survivor: The pin that stays.
        record: The values collected during the merge.
        labels: Each pin's display name, by pk, as it was before the merge.
        loser_pk: The merged-away pin's pk, for its name.
    """
    if not record.lines:
        return
    header = f"Merged {labels[loser_pk]} into {labels[survivor.pk]} on {timezone.localdate().isoformat()}. Where both pins held something different, this pin kept one; the other is below."
    entries = [f"{label}:\n{value}" if "\n" in value else f"{label}: {value}" for label, value in record.lines]
    for text in _note_texts(header, "\n\n".join(entries)):
        PinNote.objects.create(pin=survivor, text=text)


def merge_pins(survivor: Pin, loser: Pin, profile: Profile, resolutions: dict[str, int] | None = None) -> Pin:
    """
    Merge loser into survivor: reassign every relation, resolve every conflict, delete loser.
    Nothing the loser held is lost with it: see the module docstring for where each kind of data goes when the survivor already has its own.

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

    from urbanlens.dashboard.services.geo.child_pin_boundaries import deferring_child_boundary_refits

    # Captured before anything is rewritten, so the note and summaries name the pins as the user knew them.
    labels = {survivor.pk: _pin_label(survivor), loser.pk: _pin_label(loser)}
    loser_pk = loser.pk
    record = _MergeRecord()

    with transaction.atomic(), deferring_child_boundary_refits():
        _reparent_children(survivor, loser)
        _merge_aliases(survivor, loser)
        _merge_scalars(survivor, loser, record, labels)
        _merge_owners(survivor, loser)
        _merge_reviews(survivor, loser)
        _merge_list_items(survivor, loser)
        _merge_auto_removals(survivor, loser)
        _merge_shares(survivor, loser)
        _merge_custom_field_values(survivor, loser, resolutions, record, labels)
        _merge_boundaries(survivor, loser, resolutions, profile, record, labels)
        _merge_article(survivor, loser, resolutions, profile, record, labels)
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

        _merge_visits(survivor, loser)
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
        # SET_NULL relations: the delete would keep these rows but cut them loose from any pin.
        Floorplan.objects.filter(pin=loser).update(pin=survivor)
        PhotoUploadFailure.objects.filter(pin=loser).update(pin=survivor)
        PinImportFailure.objects.filter(pin=loser).update(pin=survivor)
        _repoint_other_merge_suggestions(survivor, loser)

        for markup_map in MarkupMap.objects.filter(inferred_pins=loser):
            markup_map.inferred_pins.remove(loser)
            markup_map.inferred_pins.add(survivor)

        loser.delete()
        _write_merge_note(survivor, record, labels, loser_pk)

        # last_visited is a denormalized copy of the newest PinVisit, and the visits above moved
        # across via update(), which fires no signal.
        # Recomputing also saves the survivor, which is what refreshes its cached map payload - the
        # merge has no other invalidation despite the survivor gaining visits, images and labels.
        from urbanlens.dashboard.services.visits.visits import sync_last_visited

        sync_last_visited(survivor)

    survivor.refresh_from_db()
    return survivor
