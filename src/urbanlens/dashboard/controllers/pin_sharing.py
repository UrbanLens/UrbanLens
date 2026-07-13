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
from urbanlens.dashboard.services.map_sharing import infer_source_share_for_pin, share_markup_map_with_profile
from urbanlens.dashboard.services.text_limits import MAX_PIN_SHARE_MESSAGE_LENGTH, text_length_error


def _recipient_existing_pin(profile: Profile, source: Pin) -> Pin | None:
    # A Pin's coordinates live on its Location, so "same place" == same Location.
    if not source.location_id:
        return None
    return Pin.objects.filter(
        profile=profile,
        parent_pin__isnull=True,
        location_id=source.location_id,
    ).first()


def _create_pin_from_share(share: PinShare, parent_pin: Pin | None = None) -> Pin:
    """Materialise a recipient-side Pin from an accepted share.

    Args:
        share: The accepted share to copy the pin from.
        parent_pin: When the share is part of a "pin + sub pins" bundle, the
            recipient-side pin the new pin should nest under.

    Returns:
        The newly created Pin, carrying over every user-visible property
        (name, icon, badges, notes, scores, security indicators, photos).
    """
    source = share.pin
    new_pin = Pin.objects.create(
        profile=share.to_profile,
        source_share=share,
        parent_pin=parent_pin,
        location=source.location,
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
    new_pin.badges.set(source.badges.all())
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
                "aliases": pin.aliases.all(),
                "photos": pin.images.all(),
                "child_pin_count": pin.descendants().count(),
                "maps": MarkupMap.objects.for_profile(request.user.profile).order_by("-updated"),
            },
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
        name_choice = request.POST.get("name_choice") or ""
        custom_name = (request.POST.get("custom_name") or "").strip()
        if not name_choice and custom_name:
            # The dialog's default mode is a free-text name field with no
            # explicit choice control - a typed name means "share under this".
            name_choice = "custom"
        if name_choice == "custom":
            if not custom_name:
                return HttpResponse("Enter a name, or choose one of your existing aliases.", status=400)
            length_error = text_length_error(custom_name, 255, "Name")
            if length_error:
                return HttpResponse(length_error, status=400)
            shared_name = custom_name
            # New names typed here become a permanent alias on the sharer's own
            # pin too, same as any other place the pin's name is set (see
            # Pin.save's alias-sync, which this mirrors for a name that never
            # touches pin.name itself).
            PinAlias.objects.get_or_create(pin=pin, name=custom_name)
        elif name_choice.startswith("alias:"):
            alias = get_object_or_404(PinAlias, pk=name_choice.removeprefix("alias:"), pin=pin)
            shared_name = alias.name
        # else: blank choice keeps shared_name None - "use the pin's current name".

        image_ids = request.POST.getlist("image_ids")
        selected_images = pin.images.filter(id__in=image_ids) if image_ids else Image.objects.none()

        attached_map = None
        if map_uuid := request.POST.get("markup_map_uuid"):
            attached_map = MarkupMap.objects.filter(uuid=map_uuid, profile=sender).first()

        # If this pin itself arrived via a share, record the lineage so reshare
        # chains can be counted (see PinShare.chain_share_count). Pins the
        # owner created themselves (no source_share) get a best-effort
        # heuristic link to a prior map-detected share instead, so the chain
        # doesn't silently break when someone learned about a place from a
        # shared map and then pinned + shared it themselves.
        if pin.source_share_id is None and pin.inferred_source_share_id is None:
            inferred = infer_source_share_for_pin(pin)
            if inferred is not None:
                pin.inferred_source_share = inferred
                pin.save(update_fields=["inferred_source_share", "updated"])

        already_pinned = _recipient_existing_pin(recipient, pin) is not None
        share = PinShare.objects.create(
            pin=pin,
            from_profile=sender,
            to_profile=recipient,
            parent_share_id=pin.source_share_id or pin.inferred_source_share_id,
            status=PinShareStatus.ALREADY_PINNED if already_pinned else PinShareStatus.PENDING,
            message=message,
            shared_name=shared_name,
            markup_map=attached_map,
        )
        share.images.set(selected_images)

        if attached_map is not None:
            share_markup_map_with_profile(sender, recipient, attached_map)

        # Bundle the pin's sub pins: each child pin gets its own share row
        # (counting as a share of that pin), tied to the root share. Children
        # that already have a pending share to this recipient are skipped so
        # the one-pending-share-per-pin-and-recipient constraint holds.
        bundled_count = 0
        if request.POST.get("include_children"):
            already_pending = set(
                PinShare.objects.filter(to_profile=recipient, status=PinShareStatus.PENDING, pin__in=pin.descendants()).values_list("pin_id", flat=True),
            )
            for child in pin.descendants().select_related("location"):
                if child.pk in already_pending:
                    continue
                PinShare.objects.create(
                    pin=child,
                    from_profile=sender,
                    to_profile=recipient,
                    parent_share_id=child.source_share_id,
                    bundled_with=share,
                    status=PinShareStatus.PENDING,
                )
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
    bundled = list(root_share.bundled_shares.filter(status=PinShareStatus.PENDING).select_related("pin", "pin__location"))
    if not bundled:
        return 0
    by_source_pin_id = {child_share.pin_id: child_share for child_share in bundled}
    created: dict[int, Pin] = {}
    visiting: set[int] = set()

    def materialise(child_share: PinShare) -> Pin:
        source = child_share.pin
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


class PinShareRespondView(LoginRequiredMixin, View):
    def post(self, request, share_id):
        share = get_object_or_404(PinShare.objects.select_related("pin"), pk=share_id, to_profile=request.user.profile)
        action = request.POST.get("action")
        if share.status != PinShareStatus.PENDING:
            messages.info(request, "This shared pin has already been handled.")
            return redirect("pin.share.detail", share_id=share.id)
        target_pin = None
        if action == "accept":
            with transaction.atomic():
                target_pin = _recipient_existing_pin(share.to_profile, share.pin)
                if target_pin is None:
                    target_pin = _create_pin_from_share(share)
                bundled_count = _accept_bundled_shares(share, target_pin)
                share.status = PinShareStatus.ACCEPTED
                share.save(update_fields=["status", "updated"])
            if bundled_count:
                messages.success(request, f"Pin added to your map with {bundled_count} sub pin{'s' if bundled_count != 1 else ''}.")
            else:
                messages.success(request, "Pin added to your map.")
        elif action == "reject":
            share.status = PinShareStatus.REJECTED
            share.save(update_fields=["status", "updated"])
            share.bundled_shares.filter(status=PinShareStatus.PENDING).update(status=PinShareStatus.REJECTED)
            messages.info(request, "Shared pin rejected.")
        if share.notification_id:
            NotificationLog.objects.filter(pk=share.notification_id).update(status=Status.READ)
        if request.headers.get("HX-Request"):
            from urbanlens.dashboard.controllers.notifications import _trigger_badge_refresh

            notifications = NotificationLog.objects.for_profile(request.user.profile).select_related("source_profile").order_by("-created")[:20]
            response = render(request, "dashboard/partials/notifications/notification_dropdown.html", {"notifications": notifications, "unread_count": NotificationLog.objects.for_profile(request.user.profile).unread().count()})
            return _trigger_badge_refresh(response)
        if action == "accept" and target_pin is not None:
            return redirect("pin.details", pin_slug=target_pin.slug)
        return redirect("pin.share.detail", share_id=share.id)
