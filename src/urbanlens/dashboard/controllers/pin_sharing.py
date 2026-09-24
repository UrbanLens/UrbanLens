"""Controllers for sharing a single pin with one friend."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.numbers import safe_int_or_none
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_SHARE_MESSAGE_LENGTH, text_length_error
from urbanlens.dashboard.services.sharing.pin_sharing import PinSharePermissionError, apply_pin_share_response, create_pin_from_share, create_pin_share
from urbanlens.dashboard.services.social.connections import get_connections

#: Compatibility alias for the name this helper had while it lived here.
_create_pin_from_share = create_pin_from_share


class PinShareDialogView(LoginRequiredMixin, View):
    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile=request.user.profile)

        # ?children=<uuid>,<uuid>,...
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

    GET /map/pin/<slug:pin_slug>/share/maps/

    Refetched by the dialog's "New map" flow after a map is created, so the picker gains the new
    (auto-selected) tile without reloading the rest of the already-filled-in share form.
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
        recipient = get_object_or_404(Profile, pk=safe_int_or_none(request.POST.get("profile_id")))

        message = (request.POST.get("message") or "").strip() or None
        length_error = text_length_error(message, MAX_PIN_SHARE_MESSAGE_LENGTH, "Message")
        if length_error:
            return HttpResponse(length_error, status=400)

        # Blank keeps shared_name None - "use the pin's current name".
        shared_name = (request.POST.get("custom_name") or "").strip() or None
        if shared_name:
            length_error = text_length_error(shared_name, 255, "Name")
            if length_error:
                return HttpResponse(length_error, status=400)

        attached_map = None
        if map_uuid := request.POST.get("markup_map_uuid"):
            attached_map = MarkupMap.objects.filter(uuid=map_uuid, profile=sender).first()

        selected_uuids = [u for u in request.POST.getlist("child_pin_uuids") if u]
        if selected_uuids:
            children = pin.descendants().filter(uuid__in=selected_uuids)
        elif request.POST.get("include_children"):
            children = pin.descendants()
        else:
            children = Pin.objects.none()

        try:
            create_pin_share(
                sender,
                recipient,
                pin,
                message=message,
                shared_name=shared_name,
                image_ids=[image_id for image_id in (safe_int_or_none(raw) for raw in request.POST.getlist("image_ids")) if image_id is not None],
                markup_map=attached_map,
                children=children,
            )
        except PinSharePermissionError:
            return HttpResponse("Pins can only be shared with connected friends.", status=403)
        return render(request, "dashboard/partials/pins/pin_share_dialog.html", {"pin": pin, "friends": get_connections(sender), "shared_to": recipient})


class PinShareDetailView(LoginRequiredMixin, View):
    def get(self, request, share_id):
        share = get_object_or_404(
            PinShare.objects.select_related("pin__location", "from_profile__user", "to_profile").prefetch_related("images", "bundled_shares__pin__location"),
            pk=share_id,
            to_profile=request.user.profile,
        )
        # share.safe_pin, not share.pin: a DETECTED share is a provenance record for a place the recipient
        # learned about indirectly, not an offer of the sender's pin.
        return render(
            request,
            "dashboard/pages/pin_share/detail.html",
            {"share": share, "pin": share.safe_pin, "bundled_shares": share.bundled_shares.all(), "show_map_footer": True},
        )


class PinShareRespondView(LoginRequiredMixin, View):
    def post(self, request, share_id):
        share = get_object_or_404(PinShare.objects.select_related("pin", "notification"), pk=share_id, to_profile=request.user.profile)
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
            from urbanlens.dashboard.controllers.notifications import action_taken_response

            return action_taken_response(request, request.user.profile, notification=share.notification)
        if action == "accept" and target_pin is not None:
            return redirect("pin.details", pin_slug=target_pin.slug)
        return redirect("pin.share.detail", share_id=share.id)
