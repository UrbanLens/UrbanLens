import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver

from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.reviews.model import Review

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Pin, dispatch_uid="pin_remember_child_boundary_parent")
def remember_child_boundary_parent(sender: type[Pin], instance: Pin, **kwargs) -> None:
    """Remember hierarchy/coordinate changes for the post-save boundary hook."""
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and not {"parent_pin", "parent_pin_id", "location", "location_id"}.intersection(update_fields):
        instance.child_boundary_previous_parent_id = instance.parent_pin_id
        instance.child_boundary_position_changed = False
        return
    previous = Pin.objects.filter(pk=instance.pk).values("parent_pin_id", "location_id").first() if instance.pk else None
    instance.child_boundary_previous_parent_id = previous["parent_pin_id"] if previous else None
    instance.child_boundary_position_changed = bool(
        previous
        and (previous["parent_pin_id"] != instance.parent_pin_id or previous["location_id"] != instance.location_id)
    )


@receiver(post_save, sender=Pin, dispatch_uid="pin_refit_child_boundaries_on_save")
def refit_child_boundaries_on_save(sender: type[Pin], instance: Pin, created: bool, **kwargs) -> None:
    """Keep child-generated property boundaries aligned after adds and moves."""
    if not created and not getattr(instance, "child_boundary_position_changed", False):
        return
    from urbanlens.dashboard.services.geo.child_pin_boundaries import refit_child_pin_boundary

    parent_ids = {
        parent_id
        for parent_id in (instance.parent_pin_id, instance.child_boundary_previous_parent_id)
        if parent_id is not None
    }
    for parent_id in sorted(parent_ids):
        refit_child_pin_boundary(parent_id)


@receiver(post_delete, sender=Pin, dispatch_uid="pin_refit_child_boundaries_on_delete")
def refit_child_boundaries_on_delete(sender: type[Pin], instance: Pin, **kwargs) -> None:
    """Shrink a child-generated property boundary after a child is removed."""
    from urbanlens.dashboard.services.geo.child_pin_boundaries import refit_child_pin_boundary

    refit_child_pin_boundary(instance.parent_pin_id)


@receiver(post_save, sender=Pin, dispatch_uid="pin_invalidate_map_center")
def invalidate_profile_map_center(sender: type[Pin], instance: Pin, created: bool, **kwargs) -> None:
    """Clear the cached map center so it is recomputed on the next map load."""
    if not created or not instance.profile_id:
        return
    from urbanlens.dashboard.models.profile.model import Profile

    Profile.objects.filter(pk=instance.profile_id).update(
        map_center_latitude=None,
        map_center_longitude=None,
    )


def _refresh_cached_pin(pin_id: int, profile_id: int) -> None:
    """Update one cached map pin if that profile is currently cached in Valkey."""

    def _run() -> None:
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.map_pins import MapPinCache

        try:
            profile = Profile.objects.get(pk=profile_id)
            pin = Pin.objects.get(pk=pin_id)
        except (Profile.DoesNotExist, Pin.DoesNotExist):
            try:
                MapPinCache(Profile(pk=profile_id)).delete_pin(pin_id)
            except (ConnectionError, OSError, RuntimeError):
                logger.debug("Unable to delete missing pin %s from map cache", pin_id, exc_info=True)
            return
        try:
            MapPinCache(profile).upsert_pin(pin)
        except (ConnectionError, OSError, RuntimeError):
            logger.warning("Unable to refresh cached map pin %s", pin_id, exc_info=True)

    transaction.on_commit(_run)


def _delete_cached_pin(pin_id: int, profile_id: int) -> None:
    def _run() -> None:
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.map_pins import MapPinCache

        try:
            MapPinCache(Profile(pk=profile_id)).delete_pin(pin_id)
        except (ConnectionError, OSError, RuntimeError):
            logger.warning("Unable to delete cached map pin %s", pin_id, exc_info=True)

    transaction.on_commit(_run)


@receiver(post_save, sender=Pin, dispatch_uid="pin_refresh_map_pin_cache")
def refresh_map_pin_cache(sender: type[Pin], instance: Pin, **kwargs) -> None:
    if instance.profile_id:
        _refresh_cached_pin(instance.pk, instance.profile_id)


@receiver(post_delete, sender=Pin, dispatch_uid="pin_delete_map_pin_cache")
def delete_map_pin_cache(sender: type[Pin], instance: Pin, **kwargs) -> None:
    if instance.profile_id:
        _delete_cached_pin(instance.pk, instance.profile_id)


@receiver(post_delete, sender=Pin, dispatch_uid="pin_record_tombstone")
def record_pin_tombstone(sender: type[Pin], instance: Pin, **kwargs) -> None:
    """Durably record the deletion for external-API delta-sync clients.

    Written synchronously (not ``on_commit``) so the tombstone commits or
    rolls back together with the delete itself.

    Only fires when the deletion originated on pins (a single ``pin.delete()``
    or a Pin queryset delete, including the cascade over ``parent_pin``
    children either triggers). When the deletion is a cascade from the owning
    profile/user - account deletion - no tombstones are written: the rows
    would FK a profile that is itself mid-delete (and was collected before
    they existed), and an account deletion leaves no sync clients behind to
    tell.
    """
    origin = kwargs.get("origin")
    origin_model = getattr(origin, "model", type(origin))
    if origin_model is not Pin or not instance.profile_id:
        return
    from urbanlens.dashboard.models.pin_tombstone import PinTombstone

    PinTombstone.objects.record(profile_id=instance.profile_id, pin_uuid=instance.uuid)


@receiver(m2m_changed, sender=Pin.labels.through, dispatch_uid="pin_labels_refresh_map_pin_cache")
def refresh_map_pin_cache_for_labels(sender, instance: Pin, action: str, **kwargs) -> None:
    if action in {"post_add", "post_remove", "post_clear"} and instance.profile_id:
        _refresh_cached_pin(instance.pk, instance.profile_id)


@receiver(post_save, sender=Label, dispatch_uid="label_refresh_map_pin_cache")
def refresh_map_pin_cache_for_label(sender: type[Label], instance: Label, created: bool, **kwargs) -> None:
    """A label's icon/color can appear on any pin carrying it (Pin.effective_icon).

    Unlike the m2m-add/remove case above, editing the label itself never
    touches Pin.labels.through, so nothing else here would invalidate the
    server-side Redis pin cache for pins that already carry this label - they'd
    keep serving the old baked-in icon/color until something else happened to
    touch that specific pin, or the cache TTL lapsed.
    """
    if created:
        return  # not attached to any pin yet
    for pin_id, profile_id in Pin.objects.filter(labels=instance).values_list("pk", "profile_id"):
        _refresh_cached_pin(pin_id, profile_id)


@receiver(post_save, sender=LabelCustomization, dispatch_uid="label_customization_refresh_map_pin_cache")
def refresh_map_pin_cache_for_label_customization(sender: type[LabelCustomization], instance: LabelCustomization, **kwargs) -> None:
    """Per-profile icon/color overrides need the same cache refresh as editing the label itself."""
    for pin_id in Pin.objects.filter(profile_id=instance.profile_id, labels=instance.label_id).values_list("pk", flat=True):
        _refresh_cached_pin(pin_id, instance.profile_id)


@receiver(m2m_changed, sender=Pin.labels.through, dispatch_uid="pin_labels_propagate_visited")
def propagate_visited_label_to_ancestors(sender, instance: Pin, action: str, pk_set=None, reverse: bool = False, **kwargs) -> None:
    """Mark a child pin's ancestors Visited when the child gains the Visited label.

    Visiting a child pin (an entrance, a building on a campus) means the parent
    place was visited too, so the profile's "Visited" status label cascades up
    the ``parent_pin`` chain. The whole chain is stamped in one pass with a
    cycle-safe walk (see ``Pin.ancestor_chain``); the m2m adds this performs
    re-fire this handler for each ancestor, but their ``pk_set`` only contains
    newly-added rows, so the cascade terminates once the chain is stamped.
    """
    if action != "post_add" or reverse or not pk_set or instance.parent_pin_id is None:
        return
    from urbanlens.dashboard.models.labels.model import Label

    visited_label = Label.objects.filter(pk__in=pk_set, kind="status", name="Visited").first()
    if visited_label is None:
        return
    for ancestor in instance.ancestor_chain():
        ancestor.labels.add(visited_label)


@receiver(post_save, sender=Review, dispatch_uid="review_refresh_map_pin_cache")
def refresh_map_pin_cache_for_review(sender, instance: Review, **kwargs) -> None:
    if instance.pin_id:
        _refresh_cached_pin(instance.pin_id, instance.pin.profile_id)


@receiver(post_delete, sender=Review, dispatch_uid="review_delete_refresh_map_pin_cache")
def refresh_map_pin_cache_for_deleted_review(sender, instance: Review, **kwargs) -> None:
    if instance.pin_id:
        _refresh_cached_pin(instance.pin_id, instance.pin.profile_id)


# -- Wiki-sync: mirror rating/vulnerability/priority/danger onto WikiStatVote ---
# One-way only (pin -> wiki): the wiki has no single owner, so there's no
# equivalent "wiki value" to pull back the other way - see
# Profile.sync_rating_to_wiki etc. and models.wiki_stat_vote.model.WikiStatVote's
# own docstring on why a composite average, not a single stored field, is
# the wiki-side representation of these dimensions.


def _sync_pin_stat_to_wiki(wiki_id: int, profile_id: int, field: str, value: int | None) -> None:
    """Upsert (1-5) or clear (anything else) one profile's WikiStatVote for a field.

    Mirrors WikiStatVoteView's own upsert-or-delete behavior exactly, so a
    pin's star rating going back to "unset" clears the vote the same way
    manually clearing it on the wiki page would - never leaves a stale 0/None
    row skewing the wiki's composite average.
    """

    def _run() -> None:
        from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote

        if value is not None and 1 <= value <= 5:
            WikiStatVote.objects.update_or_create(wiki_id=wiki_id, profile_id=profile_id, field=field, defaults={"value": value})
        else:
            WikiStatVote.objects.filter(wiki_id=wiki_id, profile_id=profile_id, field=field).delete()

    transaction.on_commit(_run)


@receiver(post_save, sender=Review, dispatch_uid="review_sync_rating_to_wiki")
def sync_rating_to_wiki(sender, instance: Review, **kwargs) -> None:
    if not instance.pin_id:
        return
    pin = instance.pin
    if pin.wiki_id is None or not pin.profile.sync_rating_to_wiki:
        return
    from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatField

    _sync_pin_stat_to_wiki(pin.wiki_id, pin.profile_id, WikiStatField.RATING, instance.rating)


@receiver(post_delete, sender=Review, dispatch_uid="review_sync_rating_deletion_to_wiki")
def sync_rating_deletion_to_wiki(sender, instance: Review, **kwargs) -> None:
    if not instance.pin_id:
        return
    pin = instance.pin
    if pin.wiki_id is None or not pin.profile.sync_rating_to_wiki:
        return
    from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatField

    _sync_pin_stat_to_wiki(pin.wiki_id, pin.profile_id, WikiStatField.RATING, None)


#: pin field name -> (Profile setting name, WikiStatField value)
_PIN_TO_WIKI_STAT_FIELDS = (
    ("vulnerability", "sync_vulnerability_to_wiki", "vulnerability"),
    ("priority", "sync_priority_to_wiki", "priority"),
    ("danger", "sync_danger_to_wiki", "danger"),
)


@receiver(post_save, sender=Pin, dispatch_uid="pin_sync_stats_to_wiki")
def sync_pin_stats_to_wiki(sender: type[Pin], instance: Pin, **kwargs) -> None:
    """Mirror vulnerability/priority/danger onto the pin's wiki, per-field opt-in.

    Guarded on ``update_fields`` when present (a full save touches every
    field, so nothing to filter there) to skip the profile lookup entirely on
    saves that never touched any of these three fields.
    """
    if instance.wiki_id is None:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and not any(pin_field in update_fields for pin_field, _, _ in _PIN_TO_WIKI_STAT_FIELDS):
        return
    profile = instance.profile
    for pin_field, setting_name, wiki_field in _PIN_TO_WIKI_STAT_FIELDS:
        if update_fields is not None and pin_field not in update_fields:
            continue
        if not getattr(profile, setting_name):
            continue
        _sync_pin_stat_to_wiki(instance.wiki_id, instance.profile_id, wiki_field, getattr(instance, pin_field))


@receiver(post_save, sender=Pin, dispatch_uid="pin_ensure_draft_wiki")
def ensure_draft_wiki_for_pin_location(sender: type[Pin], instance: Pin, created: bool, **kwargs) -> None:
    """Queue background creation of an unofficial draft Wiki for a newly pinned Location.

    Fires for every pin-creation path (manual add, CSV/Google Maps import,
    Flickr, Immich, GPX) since it's a model-level signal rather than a
    per-importer call - that's what makes this "still happens for bulk
    imports, but in the background" without slowing any of them down: the
    enqueue itself is a cheap non-blocking broker publish (see
    ``tasks.ensure_draft_wiki_for_location``), and the wiki stays an
    invisible draft (see ``Wiki.officially_created``) until the user
    explicitly clicks "Create Wiki" - default boundaries are still generated
    lazily on first pin-detail-page view, unchanged.

    Skipped when the triggering profile has community features disabled -
    their own action shouldn't kick off community-wiki background work for a
    location, though another profile's pin there will. Queued on_commit, like
    every other Celery-enqueuing signal in this module - a pin creation that
    ultimately rolls back should never have queued anything.
    """
    if not created or instance.location_id is None or not instance.profile.community_enabled:
        return
    location_id = instance.location_id

    def _run() -> None:
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import ensure_draft_wiki_for_location

        safely_enqueue_task(ensure_draft_wiki_for_location, location_id)

    transaction.on_commit(_run)
