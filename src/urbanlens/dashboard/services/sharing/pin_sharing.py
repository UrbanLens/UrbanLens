"""Creating a pin share, and answering one you received.
Exposure fires once at creation; acceptance records lineage via ``source_share``, never a second ``LocationExposure`` row."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.urls import reverse

from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.services.notifications.notification_delivery import send_notification_email
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.sharing.share_provenance import find_profile_pin_near_location, record_share_exposure, resolve_and_stamp_origin_share
from urbanlens.dashboard.services.social.connections import are_connections


class PinSharePermissionError(PermissionError):
    """A pin share was refused because sender and recipient aren't connected."""


if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def recipient_existing_pin(profile: Profile, source: Pin) -> Pin | None:
    """Return the recipient's own top-level pin at (or near) `source`'s place, if any.

    Args:
        profile: The prospective recipient.
        source: The pin being considered for sharing.

    Returns:
        The recipient's matching pin, or None.
    """
    if not source.location_id:
        return None
    return find_profile_pin_near_location(profile.pk, source.location)


def create_pin_share(sender: Profile, recipient: Profile, pin: Pin, *, message: str | None = None, shared_name: str | None = None) -> PinShare:
    """Create a PinShare (and its notification), enforcing the friends-only sharing rule.

    Args:
        sender: The profile sharing the pin (must own it).
        recipient: The profile the pin is being shared with.
        pin: The pin being shared.
        message: Optional note to attach.
        shared_name: Optional override name for the shared pin.

    Returns:
        The newly created PinShare.

    Raises:
        PermissionError: If `sender` and `recipient` aren't connected friends."""
    if recipient.pk == sender.pk:
        raise PinSharePermissionError(f"profile {sender.pk} attempted to share a pin with themselves")
    if not are_connections(sender, recipient):
        raise PinSharePermissionError(f"profile {sender.pk} and profile {recipient.pk} are not connected friends")

    already_pinned = recipient_existing_pin(recipient, pin) is not None
    share = PinShare.objects.create(
        pin=pin,
        location=pin.location,
        from_profile=sender,
        to_profile=recipient,
        parent_share=resolve_and_stamp_origin_share(pin),
        status=PinShareStatus.ALREADY_PINNED if already_pinned else PinShareStatus.PENDING,
        message=message,
        shared_name=shared_name,
    )
    record_share_exposure(share)
    try:
        pref = recipient.notification_preferences.pin_shared
    except AttributeError:
        pref = DeliveryPreference.SITE

    if pref != DeliveryPreference.NONE:
        sender_name = resolve_visible_identity(recipient, sender)["display_name"]
        base_message = f"{sender_name} shared {pin.display_label} with you."
        if already_pinned:
            base_message += " You already have this location pinned."
        title = "Pin shared with you"
        url = reverse("pin.share.detail", kwargs={"share_id": share.pk})

        if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
            notification = NotificationLog.objects.notify(
                profile=recipient,
                source_profile=sender,
                status=Status.UNREAD,
                importance=Importance.MEDIUM,
                notification_type=NotificationType.PIN_SHARED,
                title=title,
                message=base_message,
                url=url,
            )
            share.notification = notification
            share.save(update_fields=["notification", "updated"])
        if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
            send_notification_email(recipient, title=title, body_text=base_message, url=url)
    return share


def _official_name_for(location) -> str | None:
    """A name for a shared pin that does not come from the person sharing it.

    Args:
        location: The location the shared pin sits at, or None.

    Returns:
        An official name, or None to let the pin fall back to its location."""
    if location is None:
        return None
    if location.official_name:
        return location.official_name

    from urbanlens.dashboard.models.aliases.model import AliasSource, WikiAlias

    alias = WikiAlias.objects.filter(wiki__location=location).exclude(source=AliasSource.USER).order_by("pk").first()
    return alias.name if alias is not None else None


def create_pin_from_share(share: PinShare, parent_pin: Pin | None = None) -> Pin:
    """Materialise a recipient-side Pin from an accepted share.

    Args:
        share: The accepted share to copy the pin from.
        parent_pin: When the share is part of a "pin + child pins" bundle, the recipient-side pin the new pin should nest under.

    Returns:
        The newly created Pin, carrying over every user-visible property (name, icon, labels, notes, scores, security indicators, photos)."""
    source = share.pin
    if source is None:
        return Pin.objects.create(
            profile=share.to_profile,
            source_share=share,
            parent_pin=parent_pin,
            location=share.shared_location,
            name=share.shared_name,
            name_is_user_provided=bool(share.shared_name),
        )
    new_pin = Pin.objects.create(
        profile=share.to_profile,
        source_share=share,
        parent_pin=parent_pin,
        location=share.shared_location or source.location,
        name=share.shared_name or _official_name_for(share.shared_location or source.location),
        name_is_user_provided=bool(share.shared_name) or source.name_is_user_provided,
        pin_type=source.pin_type,
        # Copied alongside pin_type for the same reason name_is_user_provided is
        # copied alongside name: it is the only thing stopping the automatic
        # building/parcel classifier from overwriting a type the sender chose.
        pin_type_is_user_provided=source.pin_type_is_user_provided,
        # effective_icon checks custom_icon before icon, so omitting it silently
        # changed what the shared pin looked like.
        indoor_outdoor=source.indoor_outdoor,
        date_built=source.date_built,
        date_abandoned=source.date_abandoned,
        date_last_active=source.date_last_active,
        fences=source.fences,
        alarms=source.alarms,
        cameras=source.cameras,
        security=source.security,
        signs=source.signs,
        vps=source.vps,
        plywood=source.plywood,
        locked=source.locked,
    )
    # vulnerability and danger are not copied either: they read like properties of the place but
    # they are one person's rating of it, which is the owner's side of the line below.
    # The line this whole function is drawn along: facts about the *site* travel, and the owner's
    # relationship to it does not.
    shared_images = list(share.images.exclude(pending_scan=True))
    copied_images = Image.objects.bulk_create(
        [
            Image(
                image=image.image.name,
                pin=new_pin,
                location=new_pin.location,
                profile=share.to_profile,
                # Points at the sender's own stored file rather than a second copy of
                # the bytes (see this block's comment above), so it costs the recipient
                # no storage of their own to charge quota for.
                quota_exempt_reason=QuotaExemption.SHARED_COPY,
                # Both of these carry a default that quietly misdescribes the copy when omitted:
                # source would file a shared Wikimedia photo under the recipient's own uploads (and
                # into the wrong Media tab), and media_type would turn a shared video into a photo,
                # which renders as a broken image.
                source=image.source,
                media_type=image.media_type,
                media_source_key=image.media_source_key,
                media_item_key=image.media_item_key,
                author=image.author or f"Shared by {share.from_profile.username}",
                source_url=image.source_url,
                copyright=image.copyright,
                latitude=image.latitude,
                longitude=image.longitude,
                direction=image.direction,
                checksum=image.checksum,
                taken_at=image.taken_at,
                # A date, like taken_at above - not the filename it was parsed from
                # (original_filename is deliberately absent from this list; see this function's
                # docstring comment on what does and does not travel with a shared photo).
                filename_taken_at=image.filename_taken_at,
                file_size=image.file_size,
            )
            for image in shared_images
        ]
    )
    _carry_cover_photo(source, new_pin, shared_images, copied_images)
    return new_pin


def _carry_cover_photo(source: Pin, new_pin: Pin, shared_images: list[Image], copied_images: list[Image]) -> None:
    """Point the new pin's cover photo at the recipient's copy of that photo.
    The cover must never reference the sender's row, and a sender who shared only part of their gallery may not have shared the cover at all - in which case the copy correctly has none.

    Args:
        source: The sender's pin.
        new_pin: The recipient-side pin, already saved.
        shared_images: The sender's image rows, in the order they were copied.
        copied_images: The recipient's new rows, in the same order."""
    if source.cover_photo_id is None:
        return
    for original, copy in zip(shared_images, copied_images, strict=False):
        if original.pk == source.cover_photo_id:
            new_pin.cover_photo = copy
            new_pin.save(update_fields=["cover_photo", "updated"])
            return


def _accept_bundled_shares(root_share: PinShare, target_root: Pin) -> int:
    """Materialise every pending bundled child share under the accepted root.

    Args:
        root_share: The accepted root share of the bundle.
        target_root: The recipient-side pin the root share produced.

    Returns:
        Number of child pins created."""
    # Bundled child shares always carry a pin (see PinShareCreateView's
    # bundle loop) - the pin__isnull filter just makes that invariant local.
    bundled = list(root_share.bundled_shares.filter(status=PinShareStatus.PENDING, pin__isnull=False).select_related("pin", "pin__location"))
    if not bundled:
        return 0
    by_source_pin_id = {child_share.pin_id: child_share for child_share in bundled}
    created: dict[int, Pin] = {}
    visiting: set[int] = set()

    def materialise(child_share: PinShare) -> Pin:
        """Create (or reuse) the recipient-side pin for one bundled child share.

        Returns:
            The recipient-side pin for that share."""
        source = child_share.pin
        if source is None:  # pragma: no cover - excluded by the pin__isnull filter above
            return target_root
        if source.pk in created:
            return created[source.pk]
        parent = target_root
        parent_source_id = source.parent_pin_id
        # Attach under the recipient pin built for the source's own parent when
        # that parent is also in the bundle; the visiting guard degrades a
        # corrupted parent cycle to root attachment instead of recursing forever.
        if parent_source_id in by_source_pin_id and parent_source_id not in visiting and parent_source_id != root_share.pin_id:
            visiting.add(source.pk)
            parent = materialise(by_source_pin_id[parent_source_id])
            visiting.discard(source.pk)
        new_pin = create_pin_from_share(child_share, parent_pin=parent)
        created[source.pk] = new_pin
        child_share.status = PinShareStatus.ACCEPTED
        child_share.save(update_fields=["status", "updated"])
        return new_pin

    for child_share in bundled:
        materialise(child_share)
    return len(created)


def apply_pin_share_response(share: PinShare, action: str) -> tuple[Pin | None, str]:
    """Apply an accept/reject decision to a pending ``share`` and return a status message.

    Args:
        share: The share to respond to.
        action: ``"accept"`` or ``"reject"``.

    Returns:
        A ``(target_pin, message)`` tuple."""
    target_pin = None
    if action == "accept":
        with transaction.atomic():
            # The row is locked and its status re-read *inside* the transaction.
            # The caller's PENDING check happened before this call, so two retries of the same
            # accept could both pass it and both get here: each would find no recipient pin, both
            # would create one, and the (location, profile) uniqueness constraint turned the loser
            locked_status = PinShare.objects.select_for_update().filter(pk=share.pk).values_list("status", flat=True).first()
            if locked_status != PinShareStatus.PENDING:
                # Already answered - report the pin the winner produced, so a
                # retry is indistinguishable from the original success.
                share.refresh_from_db(fields=["status"])
                existing_pin = find_profile_pin_near_location(share.to_profile_id, share.shared_location)
                return existing_pin, "Pin added to your map."
            target_pin = find_profile_pin_near_location(share.to_profile_id, share.shared_location)
            if target_pin is None:
                target_pin = create_pin_from_share(share)
            bundled_count = _accept_bundled_shares(share, target_pin)
            share.status = PinShareStatus.ACCEPTED
            share.save(update_fields=["status", "updated"])
        message = f"Pin added to your map with {bundled_count} child pin{'s' if bundled_count != 1 else ''}." if bundled_count else "Pin added to your map."
    elif action == "reject":
        share.status = PinShareStatus.REJECTED
        share.save(update_fields=["status", "updated"])
        share.bundled_shares.filter(status=PinShareStatus.PENDING).update(status=PinShareStatus.REJECTED)
        message = "Shared pin rejected."
    else:
        message = "Unknown action."
    if share.notification_id:
        from urbanlens.dashboard.services.notifications.notification_center import dismiss_notification

        dismiss_notification(share.notification_id)
    return target_pin, message
