"""Safety check-in controllers: defaults, check-in CRUD, self check-in, and the contact portal."""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact
from urbanlens.dashboard.services.connections import get_connections
from urbanlens.dashboard.services.safety import (
    ContactInput,
    check_in,
    create_chat_message,
    create_checkin,
    default_contacts_as_input,
    get_or_create_preference,
    mark_found_safe,
    save_contact_defaults,
    set_checkin_contacts,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


def _parse_contacts_from_post(request: HttpRequest, profile: Profile) -> list[ContactInput]:
    """Parse a submitted contact list (friend checkboxes + free email lines) into ContactInput tuples.

    Args:
        request: Incoming HTTP request. Reads ``contact_profile_ids`` (repeated,
            friend Profile ids) and ``contact_emails`` (one address per line,
            optionally ``name <email>``).
        profile: The profile submitting the form (used to validate friend ids).

    Returns:
        List of (contact_profile, email, name) tuples.
    """
    connections_by_id = {p.pk: p for p in get_connections(profile)}
    contacts: list[ContactInput] = []

    for raw_id in request.POST.getlist("contact_profile_ids"):
        if raw_id.strip().isdigit() and int(raw_id) in connections_by_id:
            contact_profile = connections_by_id[int(raw_id)]
            contacts.append((contact_profile, None, contact_profile.username))

    for raw_line in request.POST.get("contact_emails", "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "<" in line and line.endswith(">"):
            name, _, email = line[:-1].partition("<")
            name, email = name.strip(), email.strip()
        else:
            name, email = "", line
        if email:
            contacts.append((None, email.lower(), name))

    return contacts


def _parse_grace_period(request: HttpRequest) -> datetime.timedelta:
    """Parse the submitted grace period, in hours, into a timedelta.

    Args:
        request: Incoming HTTP request. Reads ``grace_period_hours``.

    Returns:
        The parsed timedelta, defaulting to 1 hour on missing/invalid input.
    """
    try:
        hours = float(request.POST.get("grace_period_hours", "1"))
    except ValueError:
        hours = 1.0
    return datetime.timedelta(hours=max(hours, 0.25))


class SafetyHomeView(LoginRequiredMixin, View):
    """Safety defaults + check-in list.

    GET  /safety/
    POST /safety/ - update default emergency contacts and message/grace period.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the safety home page with defaults and the profile's check-ins.

        Args:
            request: Incoming HTTP request.

        Returns:
            Rendered page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        preference = get_or_create_preference(profile)
        checkins = SafetyCheckin.objects.filter(profile=profile).prefetch_related("contacts")
        return render(
            request,
            "dashboard/pages/safety/home.html",
            {
                "preference": preference,
                "checkins": checkins,
                "default_contacts": default_contacts_as_input(profile),
                "connections": get_connections(profile),
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Update the profile's safety defaults.

        Args:
            request: Incoming HTTP request.

        Returns:
            Redirect back to the safety home page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        preference = get_or_create_preference(profile)
        preference.default_message = request.POST.get("default_message", "").strip()
        preference.default_grace_period = _parse_grace_period(request)
        preference.save(update_fields=["default_message", "default_grace_period", "updated"])
        save_contact_defaults(profile, _parse_contacts_from_post(request, profile))
        return redirect("safety.home")


class SafetyCheckinCreateView(LoginRequiredMixin, View):
    """Create a new safety check-in, prefilled from the profile's defaults.

    GET  /safety/new/
    POST /safety/new/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the check-in creation form, prefilled from defaults.

        Args:
            request: Incoming HTTP request.

        Returns:
            Rendered page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        preference = get_or_create_preference(profile)
        return render(
            request,
            "dashboard/pages/safety/create.html",
            {
                "preference": preference,
                "default_contacts": default_contacts_as_input(profile),
                "connections": get_connections(profile),
                "checkin": None,
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Create the check-in and redirect to its detail page.

        Args:
            request: Incoming HTTP request.

        Returns:
            Redirect to the new check-in's detail page, or a 400 on bad input.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        error_context = {
            "preference": get_or_create_preference(profile),
            "default_contacts": default_contacts_as_input(profile),
            "connections": get_connections(profile),
            "checkin": None,
        }
        title = request.POST.get("title", "").strip()
        raw_checkin_by = request.POST.get("checkin_by", "").strip()
        if not title or not raw_checkin_by:
            return render(request, "dashboard/pages/safety/create.html", {**error_context, "error": "Title and expected check-in time are required."}, status=400)
        try:
            checkin_by = datetime.datetime.fromisoformat(raw_checkin_by)
        except ValueError:
            return render(request, "dashboard/pages/safety/create.html", {**error_context, "error": "Invalid check-in time."}, status=400)
        if checkin_by.tzinfo is None:
            checkin_by = checkin_by.replace(tzinfo=datetime.UTC)

        lat = request.POST.get("destination_latitude") or None
        lng = request.POST.get("destination_longitude") or None

        checkin = create_checkin(
            profile=profile,
            title=title,
            checkin_by=checkin_by,
            grace_period=_parse_grace_period(request),
            plan_details=request.POST.get("plan_details", "").strip(),
            contact_message=request.POST.get("contact_message", "").strip(),
            destination_latitude=lat,
            destination_longitude=lng,
            contacts=_parse_contacts_from_post(request, profile),
        )
        return redirect("safety.checkin.detail", checkin_uuid=checkin.uuid)


class SafetyCheckinDetailView(LoginRequiredMixin, View):
    """View and manage a single safety check-in (owner-only).

    GET  /safety/<uuid:checkin_uuid>/
    POST /safety/<uuid:checkin_uuid>/ - update plan/contacts, or cancel.
    """

    def get(self, request: HttpRequest, checkin_uuid: str) -> HttpResponse:
        """Render the check-in detail/monitor page.

        Args:
            request: Incoming HTTP request.
            checkin_uuid: UUID of the check-in.

        Returns:
            Rendered page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = get_object_or_404(SafetyCheckin, uuid=checkin_uuid, profile=profile)
        contacts = list(checkin.contacts.all())
        return render(
            request,
            "dashboard/pages/safety/detail.html",
            {
                "checkin": checkin,
                "contacts": contacts,
                "contacts_input": [(c.contact_profile, c.email, c.name) for c in contacts],
                "connections": get_connections(profile),
                "messages": checkin.messages.select_related("sender_profile", "sender_contact").all(),
            },
        )

    def post(self, request: HttpRequest, checkin_uuid: str) -> HttpResponse:
        """Update or cancel the check-in.

        Args:
            request: Incoming HTTP request. ``action=cancel`` cancels the
                check-in; otherwise the plan/message/contacts are updated.
            checkin_uuid: UUID of the check-in.

        Returns:
            Redirect back to the check-in detail page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = get_object_or_404(SafetyCheckin, uuid=checkin_uuid, profile=profile)

        if request.POST.get("action") == "cancel":
            from urbanlens.dashboard.services.safety import cancel_checkin

            cancel_checkin(checkin)
            return redirect("safety.home")

        checkin.title = request.POST.get("title", checkin.title).strip() or checkin.title
        checkin.plan_details = request.POST.get("plan_details", checkin.plan_details).strip()
        checkin.contact_message = request.POST.get("contact_message", checkin.contact_message).strip()
        checkin.save(update_fields=["title", "plan_details", "contact_message", "updated"])
        set_checkin_contacts(checkin, _parse_contacts_from_post(request, profile))
        return redirect("safety.checkin.detail", checkin_uuid=checkin.uuid)


class SafetyCheckinCancelView(LoginRequiredMixin, View):
    """Cancel a safety check-in (owner-only).

    POST /safety/<uuid:checkin_uuid>/cancel/
    """

    def post(self, request: HttpRequest, checkin_uuid: str) -> HttpResponse:
        """Cancel the check-in and redirect to the safety home page.

        Args:
            request: Incoming HTTP request.
            checkin_uuid: UUID of the check-in.

        Returns:
            Redirect to the safety home page.
        """
        from urbanlens.dashboard.services.safety import cancel_checkin

        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = get_object_or_404(SafetyCheckin, uuid=checkin_uuid, profile=profile)
        cancel_checkin(checkin)
        return redirect("safety.home")


class SafetyCheckinCheckInView(LoginRequiredMixin, View):
    """Self check-in link target, from the reminder email/notification.

    GET  /safety/<uuid:checkin_uuid>/checkin/ - confirmation page.
    POST /safety/<uuid:checkin_uuid>/checkin/ - actually check in.
    """

    def get(self, request: HttpRequest, checkin_uuid: str) -> HttpResponse:
        """Render a confirmation page for checking in.

        Args:
            request: Incoming HTTP request.
            checkin_uuid: UUID of the check-in.

        Returns:
            Rendered confirmation page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = get_object_or_404(SafetyCheckin, uuid=checkin_uuid, profile=profile)
        return render(request, "dashboard/pages/safety/checkin_confirm.html", {"checkin": checkin})

    def post(self, request: HttpRequest, checkin_uuid: str) -> HttpResponse:
        """Check in and redirect to the check-in detail page.

        Args:
            request: Incoming HTTP request.
            checkin_uuid: UUID of the check-in.

        Returns:
            Redirect to the check-in detail page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = get_object_or_404(SafetyCheckin, uuid=checkin_uuid, profile=profile)
        if not checkin.is_resolved:
            check_in(checkin, profile)
        return redirect("safety.checkin.detail", checkin_uuid=checkin.uuid)


class SafetyCheckinImageUploadView(LoginRequiredMixin, View):
    """Upload a photo to a safety check-in (owner-only).

    POST /safety/<uuid:checkin_uuid>/images/
    """

    def post(self, request: HttpRequest, checkin_uuid: str) -> HttpResponse:
        """Attach an uploaded image to the check-in.

        Args:
            request: Incoming HTTP request. Reads the ``image`` file field.
            checkin_uuid: UUID of the check-in.

        Returns:
            JSON with the new image's id and URL, or a 400 if no file was given.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = get_object_or_404(SafetyCheckin, uuid=checkin_uuid, profile=profile)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)
        img = Image.objects.create(image=image_file, safety_checkin=checkin, profile=profile)
        return JsonResponse({"id": img.pk, "url": img.image.url}, status=201)


class SafetyContactPortalView(View):
    """Public, token-gated view of a check-in for an emergency contact.

    GET /safety/contact/<uuid:token>/
    """

    def get(self, request: HttpRequest, token: str) -> HttpResponse:
        """Render the contact portal for a single emergency contact.

        Args:
            request: Incoming HTTP request.
            token: The contact's magic-link token.

        Returns:
            Rendered page, or 404 if the token is invalid.
        """
        contact = get_object_or_404(SafetyCheckinContact.objects.select_related("checkin", "checkin__profile"), token=token)
        checkin = contact.checkin
        return render(
            request,
            "dashboard/pages/safety/contact_portal.html",
            {
                "checkin": checkin,
                "contact": contact,
                "other_contacts": checkin.contacts.exclude(pk=contact.pk),
                "messages": checkin.messages.select_related("sender_profile", "sender_contact").all(),
            },
        )


class SafetyContactMarkSafeView(View):
    """Mark the checked-in profile as found/safe (token-gated, no login required).

    POST /safety/contact/<uuid:token>/mark-safe/
    """

    def post(self, request: HttpRequest, token: str) -> HttpResponse:
        """Mark the profile safe and redirect back to the contact portal.

        Args:
            request: Incoming HTTP request.
            token: The contact's magic-link token.

        Returns:
            Redirect back to the contact portal.
        """
        contact = get_object_or_404(SafetyCheckinContact, token=token)
        mark_found_safe(contact)
        return redirect("safety.contact.portal", token=token)


class SafetyCheckinMessageView(View):
    """No-JS fallback for check-in chat, usable by the owner (session auth) or a contact (token auth).

    Real-time delivery is handled by ``SafetyCheckinChatConsumer`` over a
    WebSocket (see ``dashboard/consumers.py``); this endpoint only exists so
    the chat form still works as a plain POST when JavaScript is unavailable.

    POST /safety/<uuid:checkin_uuid>/messages/ - owner sends a message.
    POST /safety/contact/<uuid:token>/messages/ - contact sends a message.
    """

    def post(self, request: HttpRequest, checkin_uuid: str | None = None, token: str | None = None) -> HttpResponse:
        """Post a new chat message and return the refreshed message list partial.

        Args:
            request: Incoming HTTP request. Reads ``body``.
            checkin_uuid: UUID of the check-in (owner route).
            token: Contact's magic-link token (contact route).

        Returns:
            Rendered message list partial.
        """
        checkin, contact = self._resolve(request, checkin_uuid, token)
        body = request.POST.get("body", "").strip()
        if body:
            create_chat_message(checkin, user=request.user, contact=contact, body=body)
        return render(
            request,
            "dashboard/partials/safety/_chat_panel.html",
            {"checkin": checkin, "contact": contact, "messages": checkin.messages.select_related("sender_profile", "sender_contact").all()},
        )

    def _resolve(self, request: HttpRequest, checkin_uuid: str | None, token: str | None) -> tuple[SafetyCheckin, SafetyCheckinContact | None]:
        """Resolve the check-in and, for the contact route, the authorizing contact.

        Args:
            request: Incoming HTTP request.
            checkin_uuid: UUID of the check-in (owner route), if this is the owner route.
            token: Contact's magic-link token, if this is the contact route.

        Returns:
            (checkin, contact) - contact is None on the owner route.

        Raises:
            Http404: If the owner route is used while logged out, or with a
                check-in the caller doesn't own; or the token doesn't match
                any contact.
        """
        if token is not None:
            contact = get_object_or_404(SafetyCheckinContact.objects.select_related("checkin"), token=token)
            return contact.checkin, contact
        if not request.user.is_authenticated:
            from django.http import Http404

            raise Http404
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = get_object_or_404(SafetyCheckin, uuid=checkin_uuid, profile=profile)
        return checkin, None
