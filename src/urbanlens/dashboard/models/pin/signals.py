import logging

from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

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


@receiver(m2m_changed, sender=Pin.badges.through, dispatch_uid="pin_badges_refresh_map_pin_cache")
def refresh_map_pin_cache_for_badges(sender, instance: Pin, action: str, **kwargs) -> None:
    if action in {"post_add", "post_remove", "post_clear"} and instance.profile_id:
        _refresh_cached_pin(instance.pk, instance.profile_id)


@receiver(post_save, sender=Review, dispatch_uid="review_refresh_map_pin_cache")
def refresh_map_pin_cache_for_review(sender, instance: Review, **kwargs) -> None:
    if instance.pin_id:
        _refresh_cached_pin(instance.pin_id, instance.pin.profile_id)


@receiver(post_delete, sender=Review, dispatch_uid="review_delete_refresh_map_pin_cache")
def refresh_map_pin_cache_for_deleted_review(sender, instance: Review, **kwargs) -> None:
    if instance.pin_id:
        _refresh_cached_pin(instance.pin_id, instance.pin.profile_id)


@receiver(post_save, sender=Pin, dispatch_uid="pin_enqueue_location_creation")
def enqueue_location_creation(sender: type[Pin], instance: Pin, created: bool, **kwargs) -> None:
    """Queue community wiki + boundary generation for newly-created public root pins.

    A Pin always references a Location now, so this ensures that place's community
    Wiki and default Campus boundary exist and links the pin to the wiki.
    """
    if not created or not instance.location_id or instance.is_private or instance.parent_pin_id or instance.parent_wiki_id:
        return

    def _enqueue() -> None:
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import create_location_for_pin

        safely_enqueue_task(create_location_for_pin, instance.pk)

    transaction.on_commit(_enqueue)
