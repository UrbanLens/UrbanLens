import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.reviews.model import Review

logger = logging.getLogger(__name__)


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

    Visiting a sub pin (an entrance, a building on a campus) means the parent
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


# NOTE: Pins no longer trigger any community wiki or boundary creation on save.
# Wikis are created explicitly by the user from the pin detail page, and default
# boundaries are generated lazily when a pin detail page is first viewed - so
# bulk imports create zero external API work.
