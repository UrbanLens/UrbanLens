"""Controllers for sharing a single pin with one friend."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.notifications.meta import Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.connections import are_connections, get_connections
from urbanlens.dashboard.services.map_sharing import share_markup_map_with_profile
from urbanlens.dashboard.services.share_provenance import find_profile_pin_near_location, record_share_exposure, resolve_and_stamp_origin_share
from urbanlens.dashboard.services.text_limits import MAX_PIN_SHARE_MESSAGE_LENGTH, text_length_error


def _recipient_existing_pin(profile: Profile, source: Pin) -> Pin | None:
    # A Pin's coordinates live on its Location; "same place" == same Location
    # or one within the exposure radius (see services.share_provenance).
    if not source.location_id:
        return None
    return find_profile_pin_near_location(profile.pk, source.location)


def _create_pin_from_share(share: PinShare, parent_pin: Pin | None = None) -> Pin:
    """Materialise a recipient-side Pin from an accepted share.

    Args:
        share: The accepted share to copy the pin from. Location-only shares
            (no sender pin, e.g. coordinates detected in a DM) produce a bare
            pin at the shared location instead of a property copy.
        parent_pin: When the share is part of a "pin + sub pins" bundle, the
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
    for image in share.images.all():
        Image.objects.create(
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
            checksum=image.checksum,
            taken_at=image.taken_at,
            file_size=image.file_size,
            exif_data=image.exif_data,
        )
    return new_pin


class PinShareDialogView(LoginRequiredMixin, View):
    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile=request.user.profile)
        return render(
            request,
            "dashboard/partials/pins/pin_share_dialog.html",
            {
                "pin": pin,
                "friends": get_connections(request.user.profile),
                "photos": pin.images.all(),
                "child_pin_count": pin.descendants().count(),
                "maps": MarkupMap.objects.for_profile(request.user.profile).order_by("-updated"),
            },
        )


class PinShareMapGridView(LoginRequiredMixin, View):
    """Just the pin-share dialog's map-picker tiles (see ``_pin_share_map_grid.html``).

    Refetched by the dialog's "New map" flow after a map is created, so the
    picker gains the new (auto-selected) tile without reloading the rest of
    the already-filled-in share form.

    GET /map/pin/<slug:pin_slug>/share/maps/
    """

    def get(self, request, pin_slug):
        get_object_or_404(Pin, slug=pin_slug, profile=request.user.profile)
        return render(
            request,
            "dashboard/partials/pins/_pin_share_map_grid.html",
            {"maps": MarkupMap.objects.for_profile(request.user.profile).order_by("-updated")},
        )


class PinShareCreateView(LoginRequiredMixin, View):
    def post(self, request, pin_slug):
        sender = request.user.profile
        pin = get_object_or_404(Pin, slug=pin_slug, profile=sender)
        recipient = get_object_or_404(Profile, pk=request.POST.get("profile_id"))
        if recipient == sender or not are_connections(sender, recipient):
            return HttpResponse("Pins can only be shared with connected friends.", status=403)

        message = (request.POST.get("message") or "").strip() or None
        length_error = text_length_error(message, MAX_PIN_SHARE_MESSAGE_LENGTH, "Message")
        if length_error:
            return HttpResponse(length_error, status=400)

        shared_name = None
        custom_name = (request.POST.get("custom_name") or "").strip()
        if custom_name:
            length_error = text_length_error(custom_name, 255, "Name")
            if length_error:
                return HttpResponse(length_error, status=400)
            shared_name = custom_name
            # A typed name becomes a permanent alias on the sharer's own pin
            # too, same as any other place the pin's name is set (see
            # Pin.save's alias-sync, which this mirrors for a name that never
            # touches pin.name itself).
            PinAlias.objects.get_or_create(pin=pin, name=custom_name)
        # else: blank input keeps shared_name None - "use the pin's current name".

        image_ids = request.POST.getlist("image_ids")
        selected_images = pin.images.filter(id__in=image_ids) if image_ids else Image.objects.none()

        attached_map = None
        if map_uuid := request.POST.get("markup_map_uuid"):
            attached_map = MarkupMap.objects.filter(uuid=map_uuid, profile=sender).first()

        # If this place arrived via an earlier share - the pin was accepted
        # from one, or its location carries an exposure - record the lineage
        # so reshare chains can be counted (see PinShare.chain_share_count
        # and services.share_provenance for the full resolution rule).
        parent_share = resolve_and_stamp_origin_share(pin)

        already_pinned = _recipient_existing_pin(recipient, pin) is not None
        share = PinShare.objects.create(
            pin=pin,
            location=pin.location,
            from_profile=sender,
            to_profile=recipient,
            parent_share=parent_share,
            status=PinShareStatus.ALREADY_PINNED if already_pinned else PinShareStatus.PENDING,
            message=message,
            shared_name=shared_name,
            markup_map=attached_map,
        )
        share.images.set(selected_images)
        record_share_exposure(share)

        if attached_map is not None:
            share_markup_map_with_profile(sender, recipient, attached_map)

        # Bundle the pin's sub pins: each child pin gets its own share row
        # (counting as a share of that pin), tied to the root share. Children
        # that already have a pending share to this recipient are skipped so
        # the one-pending-share-per-pin-and-recipient constraint holds.
        bundled_count = 0
        if request.POST.get("include_children"):
            already_pending = set(PinShare.objects.pending_pin_ids_for(recipient, pin.descendants()))
            for child in pin.descendants().select_related("location"):
                if child.pk in already_pending:
                    continue
                child_share = PinShare.objects.create(
                    pin=child,
                    location=child.location,
                    from_profile=sender,
                    to_profile=recipient,
                    parent_share=resolve_and_stamp_origin_share(child),
                    bundled_with=share,
                    status=PinShareStatus.PENDING,
                )
                record_share_exposure(child_share)
                bundled_count += 1

        base_message = f"{sender.username} shared {pin.display_label} with you."
        if bundled_count:
            base_message += f" It comes with {bundled_count} sub pin{'s' if bundled_count != 1 else ''}."
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
            url=reverse("pin.share.detail", kwargs={"share_id": share.id}),
        )
        share.notification = notification
        share.save(update_fields=["notification", "updated"])
        return render(request, "dashboard/partials/pins/pin_share_dialog.html", {"pin": pin, "friends": get_connections(sender), "shared_to": recipient})


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
        new_pin = _create_pin_from_share(child_share, parent_pin=parent)
        created[source.pk] = new_pin
        child_share.status = PinShareStatus.ACCEPTED
        child_share.save(update_fields=["status", "updated"])
        return new_pin

    for child_share in bundled:
        materialise(child_share)
    return len(created)


class PinShareDetailView(LoginRequiredMixin, View):
    def get(self, request, share_id):
        share = get_object_or_404(
            PinShare.objects.select_related("pin__location", "from_profile__user", "to_profile").prefetch_related("images", "bundled_shares__pin__location"),
            pk=share_id,
            to_profile=request.user.profile,
        )
        return render(
            request,
            "dashboard/pages/pin_share/detail.html",
            {"share": share, "pin": share.pin, "bundled_shares": share.bundled_shares.all(), "show_map_footer": True},
        )


def apply_pin_share_response(share: PinShare, action: str) -> tuple[Pin | None, str]:
    """Apply an accept/reject decision to a pending `share` and return a status message.

    Shared by `PinShareRespondView` (the standalone pin-share page and the
    notification dropdown) and `MessageShareRespondPinView` (the DM share
    card) so both entry points mutate the share identically.

    Args:
        share: The share to respond to. Caller must have already confirmed
            `share.status == PinShareStatus.PENDING`.
        action: ``"accept"`` or ``"reject"``.

    Returns:
        A ``(target_pin, message)`` tuple. `target_pin` is the recipient-side
        Pin on accept (None otherwise); `message` is a human-readable summary
        suitable for a toast/Django message.
    """
    target_pin = None
    if action == "accept":
        with transaction.atomic():
            target_pin = find_profile_pin_near_location(share.to_profile_id, share.shared_location)
            if target_pin is None:
                target_pin = _create_pin_from_share(share)
            bundled_count = _accept_bundled_shares(share, target_pin)
            share.status = PinShareStatus.ACCEPTED
            share.save(update_fields=["status", "updated"])
        message = f"Pin added to your map with {bundled_count} sub pin{'s' if bundled_count != 1 else ''}." if bundled_count else "Pin added to your map."
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


class PinShareRespondView(LoginRequiredMixin, View):
    def post(self, request, share_id):
        share = get_object_or_404(PinShare.objects.select_related("pin"), pk=share_id, to_profile=request.user.profile)
        action = request.POST.get("action")
        if share.status != PinShareStatus.PENDING:
            messages.info(request, "This shared pin has already been handled.")
            return redirect("pin.share.detail", share_id=share.id)
        target_pin, status_message = apply_pin_share_response(share, action)
        if action == "accept":
            messages.success(request, status_message)
        elif action == "reject":
            messages.info(request, status_message)
        if request.headers.get("HX-Request"):
            from urbanlens.dashboard.controllers.notifications import _trigger_label_refresh

            notifications = NotificationLog.objects.for_profile(request.user.profile).select_related("source_profile").order_by("-created")[:20]
            response = render(request, "dashboard/partials/notifications/notification_dropdown.html", {"notifications": notifications, "unread_count": NotificationLog.objects.for_profile(request.user.profile).unread().count()})
            return _trigger_label_refresh(response)
        if action == "accept" and target_pin is not None:
            return redirect("pin.details", pin_slug=target_pin.slug)
        return redirect("pin.share.detail", share_id=share.id)
