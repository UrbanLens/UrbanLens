"""Controllers for sharing a single pin with one friend.

The *decision* half of the lifecycle - materialising a pin from an accepted
share, re-creating its bundled child hierarchy, and flipping the share's status
- no longer lives here. It moved to ``services.sharing.pin_sharing`` so the DM share
card, the group-chat share card, this page and the external API all run the
same code; see that module's docstring, in particular its explanation of why
accepting must **not** re-record a ``LocationExposure``.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_SHARE_MESSAGE_LENGTH, text_length_error
from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity
from urbanlens.dashboard.services.sharing.map_sharing import share_markup_map_with_profile
from urbanlens.dashboard.services.sharing.pin_sharing import apply_pin_share_response, create_pin_from_share
from urbanlens.dashboard.services.sharing.share_provenance import find_profile_pin_near_location, record_share_exposure, resolve_and_stamp_origin_share
from urbanlens.dashboard.services.social.connections import are_connections, get_connections

#: Compatibility alias for the name this helper had while it lived here. Two
#: provenance test modules and ``services.profile.profile_photos``'s docstring still
#: refer to ``controllers.pin_sharing._create_pin_from_share``; keeping the name
#: bound means the move is a pure relocation rather than a rename that ripples
#: through unrelated files. New callers should import from
#: ``services.sharing.pin_sharing`` directly.
_create_pin_from_share = create_pin_from_share


def _recipient_existing_pin(profile: Profile, source: Pin) -> Pin | None:
    # A Pin's coordinates live on its Location; "same place" == same Location
    # or one within the exposure radius (see services.sharing.share_provenance).
    if not source.location_id:
        return None
    return find_profile_pin_near_location(profile.pk, source.location)


def _bundle_children(sender: Profile, recipient: Profile, root_share: PinShare, children) -> int:
    """Create one bundled child share per pin in ``children``, tied to ``root_share``.

    Shared by both bundling paths on :class:`PinShareCreateView` - "share every
    descendant" (``include_children``) and "share exactly these" (``child_pin_uuids``,
    from the detail page's multi-select toolbar). A child that already has a
    pending share to this recipient is skipped so the one-pending-share-per-
    pin-and-recipient constraint holds.

    Args:
        sender: The profile sharing the pins.
        recipient: The profile receiving them.
        root_share: The already-created root :class:`PinShare` these bundle under.
        children: The child pins to bundle.

    Returns:
        How many bundled shares were created.
    """
    children = list(children)
    already_pending = set(PinShare.objects.pending_pin_ids_for(recipient, children))
    bundled_count = 0
    for child in children:
        if child.pk in already_pending:
            continue
        child_share = PinShare.objects.create(
            pin=child,
            location=child.location,
            from_profile=sender,
            to_profile=recipient,
            parent_share=resolve_and_stamp_origin_share(child),
            bundled_with=root_share,
            status=PinShareStatus.PENDING,
        )
        record_share_exposure(child_share)
        bundled_count += 1
    return bundled_count


class PinShareDialogView(LoginRequiredMixin, View):
    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile=request.user.profile)

        # ?children=<uuid>,<uuid>,... - the detail-page multi-select bulk
        # toolbar's "Share" action opens this same dialog with a specific
        # subset of child pins pre-chosen, rather than the plain "share
        # everything nested under this pin" checkbox shown otherwise.
        selected_child_pins: list[Pin] = []
        if raw_uuids := request.GET.get("children"):
            uuids = [u for u in raw_uuids.split(",") if u]
            selected_child_pins = list(pin.descendants().filter(uuid__in=uuids).select_related("location"))

        return render(
            request,
            "dashboard/partials/pins/pin_share_dialog.html",
            {
                "pin": pin,
                "friends": get_connections(request.user.profile),
                "photos": pin.images.all(),
                "child_pin_count": pin.descendants().count(),
                "selected_child_pins": selected_child_pins,
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
            # touches pin.name itself). Case-insensitive lookup matches the
            # alias uniqueness rule, so re-sharing under a different casing of
            # an existing alias reuses that row instead of raising IntegrityError.
            PinAlias.objects.get_or_create(pin=pin, name__iexact=custom_name, defaults={"name": custom_name})
        # else: blank input keeps shared_name None - "use the pin's current name".

        image_ids = request.POST.getlist("image_ids")
        selected_images = pin.images.filter(id__in=image_ids) if image_ids else Image.objects.none()

        attached_map = None
        if map_uuid := request.POST.get("markup_map_uuid"):
            attached_map = MarkupMap.objects.filter(uuid=map_uuid, profile=sender).first()

        # If this place arrived via an earlier share - the pin was accepted
        # from one, or its location carries an exposure - record the lineage
        # so reshare chains can be counted (see PinShare.chain_share_count
        # and services.sharing.share_provenance for the full resolution rule).
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

        # Bundle child pins: each gets its own share row (counting as a share
        # of that pin), tied to the root share. A specific selection - from
        # the detail page's multi-select toolbar - takes precedence over the
        # dialog's own "share everything nested under this pin" checkbox; a
        # pin can only ever come from one of the two paths in a single submit.
        selected_uuids = [u for u in request.POST.getlist("child_pin_uuids") if u]
        if selected_uuids:
            bundled_count = _bundle_children(sender, recipient, share, pin.descendants().filter(uuid__in=selected_uuids).select_related("location"))
        elif request.POST.get("include_children"):
            bundled_count = _bundle_children(sender, recipient, share, pin.descendants().select_related("location"))
        else:
            bundled_count = 0

        try:
            pref = recipient.notification_preferences.pin_shared
        except AttributeError:
            pref = DeliveryPreference.SITE

        if pref != DeliveryPreference.NONE:
            sender_name = resolve_visible_identity(recipient, sender)["display_name"]
            base_message = f"{sender_name} shared {pin.display_label} with you."
            if bundled_count:
                base_message += f" It comes with {bundled_count} child pin{'s' if bundled_count != 1 else ''}."
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
                url=reverse("pin.share.detail", kwargs={"share_id": share.id}),
            )
            share.notification = notification
            share.save(update_fields=["notification", "updated"])
        return render(request, "dashboard/partials/pins/pin_share_dialog.html", {"pin": pin, "friends": get_connections(sender), "shared_to": recipient})


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
        target_pin, status_message = apply_pin_share_response(share, action)
        if action == "accept":
            messages.success(request, status_message)
        elif action == "reject":
            messages.info(request, status_message)
        if request.headers.get("HX-Request"):
            from urbanlens.dashboard.controllers.notifications import _trigger_label_refresh

            notifications = NotificationLog.objects.for_profile(request.user.profile).for_display().order_by("-created")[:20]
            response = render(request, "dashboard/partials/notifications/notification_dropdown.html", {"notifications": notifications, "unread_count": NotificationLog.objects.for_profile(request.user.profile).unread().count()})
            return _trigger_label_refresh(response)
        if action == "accept" and target_pin is not None:
            return redirect("pin.details", pin_slug=target_pin.slug)
        return redirect("pin.share.detail", share_id=share.id)
