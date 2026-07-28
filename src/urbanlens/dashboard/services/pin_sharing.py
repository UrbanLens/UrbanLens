"""Creating a pin share, and answering one you received.

Two halves of one lifecycle:

**Creation.** The standalone share dialog
(``controllers.pin_sharing.PinShareCreateView``) has its own richer flow
(custom names, bundled child pins, photo selection) and is left as-is;
:func:`create_pin_share` holds just the create-and-notify core so a second,
simpler caller (chat) doesn't have to duplicate the friends-only rule or the
notification wiring.

**Response.** :func:`apply_pin_share_response` was extracted from
``controllers.pin_sharing`` so the surfaces that let a recipient answer a share
- the standalone share page and notification dropdown
(``controllers.pin_sharing.PinShareRespondView``), the DM share card
(``controllers.direct_message_shares``), the group-chat share card
(``controllers.group_chats``) and the external API - all mutate the share
through one implementation. The decision is not a formality: accepting
materialises a real Pin on the recipient's map, re-creates the sharer's whole
child hierarchy underneath it, and stamps the provenance link that every future
reshare of that place chains under. Four copies of that would drift, and the
first symptom would be a broken exposure chain nobody can reconstruct after the
fact.

**The provenance rule, stated here because getting it wrong is silent.**

The project-wide instruction is that any new pin/location share path calls
``resolve_origin_share`` + ``record_share_exposure``. That rule is about *share
creation* - the moment a place is revealed to someone who could not previously
see it - and :func:`create_pin_share` duly obeys it. Accepting is **not** that
moment: the exposure already fired when the ``PinShare`` row was created,
because the recipient learned the location existed as soon as the share landed
in their inbox, whether or not they ever pressed Accept.

Calling it again on accept would write a *second* ``LocationExposure`` row for
the same (profile, location) pair, and
``services.share_provenance.resolve_origin_share`` resolves lineage by picking
the **earliest** exposure. A duplicate is therefore not merely redundant
book-keeping: it inflates exposure counts and, once the original is ever
removed or the ordering ties, it can re-parent a reshare chain onto the wrong
ancestor - corrupting exactly the audit trail the rule exists to protect.

Acceptance records lineage the other way instead: the new Pin carries
``source_share`` (set in :func:`create_pin_from_share`), which is the first
thing ``resolve_origin_share`` consults. The chain is complete without a second
exposure row, and any future change here must keep the number of
``LocationExposure`` rows unchanged across an accept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.urls import reverse

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.services.connections import are_connections
from urbanlens.dashboard.services.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.share_provenance import find_profile_pin_near_location, record_share_exposure, resolve_and_stamp_origin_share

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
        PermissionError: If `sender` and `recipient` aren't connected friends.
    """
    if recipient.pk == sender.pk or not are_connections(sender, recipient):
        raise PermissionError("Pins can only be shared with connected friends.")

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
        notification = NotificationLog.objects.create(
            profile=recipient,
            source_profile=sender,
            status=Status.UNREAD,
            importance=Importance.MEDIUM,
            notification_type=NotificationType.PIN_SHARED,
            title="Pin shared with you",
            message=base_message,
            url=reverse("pin.share.detail", kwargs={"share_id": share.pk}),
        )
        share.notification = notification
        share.save(update_fields=["notification", "updated"])
    return share


def create_pin_from_share(share: PinShare, parent_pin: Pin | None = None) -> Pin:
    """Materialise a recipient-side Pin from an accepted share.

    ``source_share`` is set on the new pin, and that is what makes the
    recipient's own future shares of this place chain under the share they
    received it through - see this module's docstring for why that, rather than
    a second ``LocationExposure`` row, is how acceptance records lineage.

    Args:
        share: The accepted share to copy the pin from. Location-only shares
            (no sender pin, e.g. coordinates detected in a DM) produce a bare
            pin at the shared location instead of a property copy.
        parent_pin: When the share is part of a "pin + child pins" bundle, the
            recipient-side pin the new pin should nest under.

    Returns:
        The newly created Pin, carrying over every user-visible property
        (name, icon, labels, notes, scores, security indicators, photos).
    """
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
        name=share.shared_name or source.name,
        name_is_user_provided=bool(share.shared_name) or source.name_is_user_provided,
        icon=source.icon,
        description=source.description,
        priority=source.priority,
        vulnerability=source.vulnerability,
        danger=source.danger,
        pin_type=source.pin_type,
        color=source.color,
        detail_bg_color=source.detail_bg_color,
        detail_bg_opacity=source.detail_bg_opacity,
        detail_border_color=source.detail_border_color,
        detail_border_opacity=source.detail_border_opacity,
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
    new_pin.labels.set(source.labels.all())
    Image.objects.bulk_create(
        Image(
            image=image.image.name,
            pin=new_pin,
            location=new_pin.location,
            profile=share.to_profile,
            caption=image.caption,
            author=image.author,
            source_url=image.source_url,
            copyright=image.copyright,
            latitude=image.latitude,
            longitude=image.longitude,
            direction=image.direction,
            checksum=image.checksum,
            taken_at=image.taken_at,
            file_size=image.file_size,
            exif_data=image.exif_data,
        )
        for image in share.images.all()
    )
    return new_pin


def _accept_bundled_shares(root_share: PinShare, target_root: Pin) -> int:
    """Materialise every pending bundled child share under the accepted root.

    Recreates the sharer's parent/child hierarchy on the recipient's side:
    each bundled share's new pin nests under the recipient pin created for its
    source parent (or directly under the accepted root when the parent was the
    shared pin itself, or wasn't part of the bundle).

    Args:
        root_share: The accepted root share of the bundle.
        target_root: The recipient-side pin the root share produced.

    Returns:
        Number of child pins created.
    """
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

        Args:
            child_share: The bundled share to materialise.

        Returns:
            The recipient-side pin for that share.
        """
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

    The single mutation path for every surface that lets a recipient answer a
    share (see this module's docstring for the list).

    It performs **no** authorization of its own: callers must have already
    established that the responding profile is ``share.to_profile``, and should
    do so by scoping the lookup itself
    (``get_object_or_404(PinShare, pk=..., to_profile=...)``) rather than with a
    separate permission branch - both because an id belonging to someone else
    must be indistinguishable from one that does not exist, and because
    ``PinShare`` ids are sequential integers, so an unscoped caller here is a
    cross-tenant *write* reachable by counting.

    Deliberately does not call ``record_share_exposure``: that already fired at
    share creation, and a second call would duplicate the ``LocationExposure``
    row ``resolve_origin_share`` uses to pick a chain's ancestor. See this
    module's docstring - it is the most tempting wrong change in this file, and
    ``test_external_api_pin_shares`` asserts the row count is unchanged across
    an accept specifically to catch it.

    Args:
        share: The share to respond to. Caller must have already confirmed
            ``share.status == PinShareStatus.PENDING``.
        action: ``"accept"`` or ``"reject"``. Anything else is a no-op reported
            as "Unknown action." rather than an exception, because the internal
            HTMX callers post a raw form field and a typo there must not 500.

    Returns:
        A ``(target_pin, message)`` tuple. ``target_pin`` is the recipient-side
        Pin on accept (None otherwise); ``message`` is a human-readable summary
        suitable for a toast/Django message.
    """
    target_pin = None
    if action == "accept":
        with transaction.atomic():
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
        NotificationLog.objects.filter(pk=share.notification_id).update(status=Status.READ)
    return target_pin, message
