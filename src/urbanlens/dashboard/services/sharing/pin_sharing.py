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
``services.sharing.share_provenance.resolve_origin_share`` resolves lineage by picking
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
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.sharing.share_provenance import find_profile_pin_near_location, record_share_exposure, resolve_and_stamp_origin_share
from urbanlens.dashboard.services.social.connections import are_connections


class PinSharePermissionError(PermissionError):
    """A pin share was refused because sender and recipient aren't connected.

    ``safe_message`` is safe to surface directly to the caller.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


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
        raise PinSharePermissionError("Pins can only be shared with connected friends.")

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
        notification = NotificationLog.objects.notify(
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


def _official_name_for(location) -> str | None:
    """A name for a shared pin that does not come from the person sharing it.

    What somebody called their own pin is their business - "my old school" says
    more about them than about the building. So the sharer either names the share
    deliberately (``PinShare.shared_name``) or the copy falls back to what the
    outside world calls the place: the location's externally-sourced
    ``official_name``, else an alias on its wiki that came from a provider rather
    than from a person.

    User-authored aliases are excluded for the same reason the pin's own name is.

    Args:
        location: The location the shared pin sits at, or None.

    Returns:
        An official name, or None to let the pin fall back to its location.
    """
    if location is None:
        return None
    if location.official_name:
        return location.official_name

    from urbanlens.dashboard.models.aliases.model import AliasSource, WikiAlias

    alias = WikiAlias.objects.filter(wiki__location=location).exclude(source=AliasSource.USER).order_by("pk").first()
    return alias.name if alias is not None else None


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
    # vulnerability and danger are not copied either: they read like properties of
    # the place but they are one person's rating of it, which is the owner's side
    # of the line below.
    #
    # The line this whole function is drawn along: facts about the *site* travel,
    # and the owner's relationship to it does not. Its dates and what was observed
    # there (fences, alarms, cameras, security, signs, vps, plywood, locked)
    # describe the place and are what a share is for. Notes, styling, icon,
    # colour, priority and labels are one person's account of it, and stay with
    # them.
    #
    # The description never travels. It is the owner's personal notes about the
    # place - what they thought, what they saw, what they mean to do about it -
    # and there is no way in the product for somebody to consent to passing that
    # on. Absent a way to ask, the answer is no.
    #
    # Not the sharer's labels. A Label belongs to one profile, so setting them
    # here hung the *sharer's* rows off the *recipient's* pin - which shows one
    # person's private organising scheme to another and leaves the recipient
    # holding references they cannot manage. Labels are per-user by design;
    # copying them is not a privacy question so much as a category error.
    #
    # The pin's own styling is not copied either: how somebody chose to colour
    # their pin is theirs, not part of the place. What travels is what is true
    # about the site - its dates, and what was observed there (fences, alarms,
    # cameras, security, signs, vps, plywood, locked).
    # What travels with a photo, and what does not.
    #
    # Not the caption, and not exif_data: the sharer chose to show somebody a
    # picture, which is not the same as handing over what they wrote about it or
    # the block behind it - camera serial, timestamps, and the coordinates the
    # shot was taken from. Consent to the image is not consent to its file.
    #
    # Dates, lat/lng and direction do travel, because they are what let the copy
    # be placed on a map and set in time - the reasons a photo is worth sharing.
    #
    # An unattributed photo gets the sharer's name, so the recipient can see where
    # it came from; one that already credits somebody keeps that credit.
    shared_images = list(share.images.all())
    copied_images = Image.objects.bulk_create(
        [
            Image(
                image=image.image.name,
                pin=new_pin,
                location=new_pin.location,
                profile=share.to_profile,
                # Both of these carry a default that quietly misdescribes the copy when
                # omitted: source would file a shared Wikimedia photo under the
                # recipient's own uploads (and into the wrong Media tab), and media_type
                # would turn a shared video into a photo, which renders as a broken image.
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
                file_size=image.file_size,
            )
            for image in shared_images
        ]
    )
    _carry_cover_photo(source, new_pin, shared_images, copied_images)
    return new_pin


def _carry_cover_photo(source: Pin, new_pin: Pin, shared_images: list[Image], copied_images: list[Image]) -> None:
    """Point the new pin's cover photo at the recipient's copy of that photo.

    The cover must never reference the sender's row, and a sender who shared only
    part of their gallery may not have shared the cover at all - in which case the
    copy correctly has none.

    Args:
        source: The sender's pin.
        new_pin: The recipient-side pin, already saved.
        shared_images: The sender's image rows, in the order they were copied.
        copied_images: The recipient's new rows, in the same order.
    """
    if source.cover_photo_id is None:
        return
    for original, copy in zip(shared_images, copied_images, strict=False):
        if original.pk == source.cover_photo_id:
            new_pin.cover_photo = copy
            new_pin.save(update_fields=["cover_photo", "updated"])
            return


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
        share: The share to respond to. Callers still check
            ``share.status == PinShareStatus.PENDING`` first for the error
            message, but that check is advisory - accept re-reads the status
            under a row lock, so a share answered concurrently replays rather
            than double-applying.
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
            # The row is locked and its status re-read *inside* the
            # transaction. The caller's PENDING check happened before this
            # call, so two retries of the same accept could both pass it and
            # both get here: each would find no recipient pin, both would
            # create one, and the (location, profile) uniqueness constraint
            # turned the loser into an uncaught IntegrityError - while bundled
            # children could be materialized twice when a target pin already
            # existed. Re-reading under the lock makes the second accept a
            # no-op replay instead.
            # Only the *status* is read back, and `share` is deliberately not
            # rebound to the locked instance: callers hold this object and read
            # `share.status` from it after this returns, so the accepted state
            # has to land on the instance they already have.
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
        NotificationLog.objects.filter(pk=share.notification_id).update(status=Status.READ)
    return target_pin, message
