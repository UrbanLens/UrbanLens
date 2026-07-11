"""Safety check-in controllers: defaults, check-in CRUD, self check-in, and the contact portal."""

from __future__ import annotations

import datetime
from decimal import Decimal
import json
import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinContact, SafetyContactOptOutScope
from urbanlens.dashboard.services.connections import get_connections
from urbanlens.dashboard.services.images import image_to_gallery_json
from urbanlens.dashboard.services.map_snapshot import default_markup_map_title
from urbanlens.dashboard.services.pagination import get_page
from urbanlens.dashboard.services.safety import (
    ContactInput,
    check_in,
    create_chat_message,
    create_checkin,
    default_contacts_as_input,
    find_community_wiki,
    get_active_checkin,
    get_or_create_preference,
    is_contact_opted_out,
    mark_found_safe,
    notify_contacts_of_update,
    record_contact_opt_out,
    save_contact_defaults,
    set_checkin_contacts,
    validate_notifiable_contacts,
    wiki_notify_stats,
)
from urbanlens.dashboard.services.undo.service import stash_for_undo

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.http import HttpRequest

logger = logging.getLogger(__name__)

_GALLERY_PAGE_SIZE = 12

# Safety check-in maps offer the same base layers as the shared map composer
# (street / satellite / topo, plus the borders overlay). Attribution is
# rendered as static text in the page footer covering all of them, matching
# the main map's "attributionControl: false" + footer-attribution convention.
_MAP_ATTRIBUTION = "© OpenStreetMap contributors · Tiles © Esri · © OpenTopoMap (CC-BY-SA) · Leaflet"


def _markup_style_context(profile: Profile) -> dict:
    """Return the profile's markup color/opacity defaults for the markup panel.

    Shared with the pin detail and location wiki pages' map context, so a
    check-in's route/plan markup starts from the same fill/border defaults
    the user already has configured elsewhere.

    Args:
        profile: The requesting user's Profile instance.

    Returns:
        Dict of markup_fill_color/markup_fill_opacity/markup_border_color/markup_border_opacity.
    """
    return {
        "markup_fill_color": profile.markup_fill_color,
        "markup_fill_opacity": profile.markup_fill_opacity,
        "markup_border_color": profile.markup_border_color,
        "markup_border_opacity": profile.markup_border_opacity,
    }


def _ensure_markup_map(checkin: SafetyCheckin, profile: Profile) -> MarkupMap:
    """Return the check-in's route MarkupMap, creating and linking one if missing.

    The map is seeded with the destination as its centre so the route-drawing
    toolbar opens on the right spot.

    Args:
        checkin: The check-in needing a route map.
        profile: The check-in owner (becomes the map owner).

    Returns:
        The (possibly just-created) MarkupMap linked to the check-in.
    """
    if checkin.markup_map is not None:
        return checkin.markup_map
    markup_map = MarkupMap.objects.create(
        profile=profile,
        title=default_markup_map_title(),
        center_latitude=float(checkin.destination_latitude) if checkin.destination_latitude is not None else None,
        center_longitude=float(checkin.destination_longitude) if checkin.destination_longitude is not None else None,
        zoom=13,
    )
    checkin.markup_map = markup_map
    checkin.save(update_fields=["markup_map"])
    return markup_map


def _parse_contacts_from_post(request: HttpRequest, profile: Profile) -> list[ContactInput]:
    """Parse a submitted contact list (friend chips + email chips) into ContactInput tuples.

    Args:
        request: Incoming HTTP request. Reads ``contact_profile_ids`` (repeated,
            friend Profile ids) and ``contact_emails`` (repeated, one address per
            chip, optionally ``name <email>``).
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

    for raw_line in request.POST.getlist("contact_emails"):
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


def _get_checkin_by_slug(profile: Profile, checkin_slug: str) -> SafetyCheckin:
    """Look up an owner's check-in by slug, falling back to UUID.

    Mirrors the Pin controller's slug-then-uuid lookup: the URL kwarg is
    usually a real slug, but older/direct-linked check-ins may still be
    identified by their raw UUID.

    Args:
        profile: The check-in's owner (only their own check-ins match).
        checkin_slug: The `<slug:checkin_slug>` value captured from the URL.

    Returns:
        The matching SafetyCheckin.

    Raises:
        Http404: If neither a slug nor a UUID match.
    """
    try:
        return SafetyCheckin.objects.get(slug=checkin_slug, profile=profile)
    except SafetyCheckin.DoesNotExist:
        try:
            return get_object_or_404(SafetyCheckin, uuid=checkin_slug, profile=profile)
        except ValidationError as exc:
            raise Http404 from exc


def _contact_display_label(contact_profile: Profile | None, email: str | None, label: str) -> str:
    """Return the best display label for a saved contact, for the defaults summary.

    Args:
        contact_profile: The linked connection, if the contact is a friend.
        email: The raw email address, if the contact isn't a linked friend.
        label: A custom label saved alongside the contact, if any.

    Returns:
        The friend's username, else the custom label, else the raw email.
    """
    if contact_profile is not None:
        return contact_profile.username
    return label or email or ""


def _contact_status_map(checkin: SafetyCheckin, contacts: Iterable[SafetyCheckinContact]) -> dict[int | str, dict[str, str]]:
    """Build a {contact identity -> {"label", "class"}} map for the contact-picker's status badges.

    Args:
        checkin: The check-in these contacts belong to (used to resolve opt-out scopes).
        contacts: The check-in's contacts (avoids re-querying ``checkin.contacts``).

    Returns:
        Dict keyed by ``contact_profile_id`` (int) or ``email`` (str) - matching the lookup key
        ``_contact_picker.html`` computes via ``contact_profile.id|default:email``.
    """
    status_map: dict[int | str, dict[str, str]] = {}
    for contact in contacts:
        if contact.found_safe_at:
            label, css_class = "Found you", "safety-badge--success"
        elif is_contact_opted_out(contact.contact_profile, contact.email, owner=checkin.profile, checkin=checkin):
            label, css_class = "Opted out", "safety-badge--muted"
        elif contact.notified_at:
            label, css_class = "Notified", "safety-badge--warning"
        else:
            label, css_class = "Not yet notified", ""
        key = contact.contact_profile_id or contact.email
        if key is None:
            # A contact with neither a profile nor an email cannot be keyed.
            continue
        status_map[key] = {"label": label, "class": css_class}
    return status_map


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


def _parse_auto_delete_days(request: HttpRequest) -> int | None:
    """Parse the submitted auto-delete window, in days.

    Args:
        request: Incoming HTTP request. Reads ``auto_delete_after_days`` - absent or
            blank means "never delete" (the ``auto_delete_never`` checkbox, when
            checked, clears the number field client-side before submit).

    Returns:
        A positive day count, or None for "never".
    """
    raw = request.POST.get("auto_delete_after_days", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


class SafetyActiveCheckinBannerView(LoginRequiredMixin, View):
    """Navbar banner for the profile's currently active (unresolved) check-in, if any.

    Loaded via HTMX from the site-wide navbar (see partials/layout/header.html)
    on every page, so it stays in sync without every page's view needing to
    fetch and pass the active check-in itself.

    GET /safety/nav-banner/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the active check-in banner partial.

        Args:
            request: Incoming HTTP request.

        Returns:
            Rendered banner partial - empty when the profile has no active check-in.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return render(request, "dashboard/partials/safety/_active_checkin_banner.html", {"checkin": get_active_checkin(profile)})


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
                "active_checkin": get_active_checkin(profile),
                "default_contacts": default_contacts_as_input(profile),
                "connections": get_connections(profile),
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        """Update the profile's safety defaults.

        Called both as a plain form submit and, from the defaults form's
        autosave behavior, as an XHR request - distinguished by the
        ``X-Requested-With`` header set by the autosave JS.

        Args:
            request: Incoming HTTP request.

        Returns:
            For an XHR autosave request, a JSON summary of the saved defaults.
            Otherwise, a redirect back to the safety home page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        preference = get_or_create_preference(profile)
        preference.default_message = request.POST.get("default_message", "").strip()
        preference.default_grace_period = _parse_grace_period(request)
        preference.auto_delete_after_days = _parse_auto_delete_days(request)
        preference.save(update_fields=["default_message", "default_grace_period", "auto_delete_after_days", "updated"])
        allowed, rejected = validate_notifiable_contacts(profile, _parse_contacts_from_post(request, profile))
        save_contact_defaults(profile, allowed)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "default_message": preference.default_message,
                    "default_grace_period_display": preference.default_grace_period_display,
                    "auto_delete_after_days": preference.auto_delete_after_days,
                    "contact_labels": [_contact_display_label(*contact) for contact in default_contacts_as_input(profile)],
                    "rejected_contacts": rejected,
                }
            )
        for message in rejected:
            messages.error(request, message)
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
            Rendered page, or a redirect to the profile's already-active
            check-in - only one may be active at a time.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        active_checkin = get_active_checkin(profile)
        if active_checkin is not None:
            messages.info(request, "You already have an active check-in - check in or cancel it before starting a new one.")
            return redirect("safety.checkin.detail", checkin_slug=active_checkin.slug or active_checkin.uuid)
        preference = get_or_create_preference(profile)
        return render(
            request,
            "dashboard/pages/safety/create.html",
            {
                "preference": preference,
                "default_contacts": default_contacts_as_input(profile),
                "connections": get_connections(profile),
                "checkin": None,
                "map_attribution": _MAP_ATTRIBUTION,
                **_markup_style_context(profile),
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
            "map_attribution": _MAP_ATTRIBUTION,
            **_markup_style_context(profile),
        }
        raw_checkin_by = request.POST.get("checkin_by", "").strip()
        if not raw_checkin_by:
            return render(request, "dashboard/pages/safety/create.html", {**error_context, "error": "Expected check-in time is required."}, status=400)
        try:
            checkin_by = datetime.datetime.fromisoformat(raw_checkin_by)
        except ValueError:
            return render(request, "dashboard/pages/safety/create.html", {**error_context, "error": "Invalid check-in time."}, status=400)
        if checkin_by.tzinfo is None:
            checkin_by = checkin_by.replace(tzinfo=datetime.UTC)
        if checkin_by <= timezone.now():
            return render(request, "dashboard/pages/safety/create.html", {**error_context, "error": "Expected check-in time must be in the future."}, status=400)

        title = request.POST.get("title", "").strip() or f"Check-in - {checkin_by:%b} {checkin_by.day}, {checkin_by.year}"

        lat = request.POST.get("destination_latitude") or None
        lng = request.POST.get("destination_longitude") or None

        allowed_contacts, rejected_contacts = validate_notifiable_contacts(profile, _parse_contacts_from_post(request, profile))
        if rejected_contacts:
            return render(request, "dashboard/pages/safety/create.html", {**error_context, "error": " ".join(rejected_contacts)}, status=400)

        try:
            checkin = create_checkin(
                profile=profile,
                title=title,
                checkin_by=checkin_by,
                grace_period=_parse_grace_period(request),
                plan_details=request.POST.get("plan_details", "").strip(),
                contact_message=request.POST.get("contact_message", "").strip(),
                destination_latitude=float(lat) if lat else None,
                destination_longitude=float(lng) if lng else None,
                contacts=allowed_contacts,
                notify_community_wiki="notify_community_wiki" in request.POST,
            )
        except ValueError as exc:
            return render(request, "dashboard/pages/safety/create.html", {**error_context, "error": str(exc)}, status=400)
        self._link_markup_map(request, profile, checkin)
        return redirect("safety.checkin.detail", checkin_slug=checkin.slug)

    def _link_markup_map(self, request: HttpRequest, profile: Profile, checkin: SafetyCheckin) -> None:
        """Attach the draft MarkupMap drawn on the creation page, if any.

        The creation page lazily creates a standalone map the moment the user
        starts drawing route markup (see ``_markup_toolbar_script.html``) and
        submits its uuid in the ``markup_map`` field; here it becomes the new
        check-in's route map. Only the caller's own unattached maps qualify -
        a stale/foreign uuid is ignored rather than failing the check-in.

        Args:
            request: The creation-form POST request.
            profile: The check-in owner.
            checkin: The freshly created check-in.
        """
        map_uuid = request.POST.get("markup_map", "").strip()
        if not map_uuid:
            return
        try:
            markup_map = MarkupMap.objects.for_profile(profile).unattached().filter(uuid=map_uuid).first()
        except (ValidationError, ValueError):
            markup_map = None
        if markup_map is None:
            logger.warning("Ignoring markup_map %r on check-in create: not an unattached map owned by profile %s", map_uuid, profile.pk)
            return
        checkin.markup_map = markup_map
        checkin.save(update_fields=["markup_map"])


class SafetyCheckinDetailView(LoginRequiredMixin, View):
    """View and manage a single safety check-in (owner-only).

    GET  /safety/<slug:checkin_slug>/
    POST /safety/<slug:checkin_slug>/ - update plan/contacts, or cancel.
    """

    def get(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Render the check-in detail/monitor page, or the community view for non-owners.

        A non-owner following the link from a community wiki comment (see
        ``services.safety.post_checkin_to_community_wiki``) gets a limited
        read-only status page instead of a 404 - but only for check-ins that
        were actually posted to a wiki, and only via their UUID link.

        Args:
            request: Incoming HTTP request.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            Rendered page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            checkin = _get_checkin_by_slug(profile, checkin_slug)
        except Http404:
            return self._render_community_view(request, checkin_slug)
        checkin.ensure_slug()
        _ensure_markup_map(checkin, profile)
        contacts = list(checkin.contacts.all())
        destination_wiki = find_community_wiki(checkin.destination_latitude, checkin.destination_longitude)
        last_wiki_edit, wiki_editor_count = wiki_notify_stats(destination_wiki) if destination_wiki else (None, 0)
        return render(
            request,
            "dashboard/pages/safety/detail.html",
            {
                "checkin": checkin,
                "contacts": contacts,
                "contacts_input": [(c.contact_profile, c.email, c.name) for c in contacts],
                "contact_status": _contact_status_map(checkin, contacts),
                "connections": get_connections(profile),
                "messages": checkin.messages.select_related("sender_profile", "sender_contact").all(),
                "destination_wiki": destination_wiki,
                "last_wiki_edit": last_wiki_edit,
                "wiki_editor_count": wiki_editor_count,
                "attached_maps": checkin.markup_maps.all(),
                "map_attribution": _MAP_ATTRIBUTION,
                **_markup_style_context(profile),
            },
        )

    def _render_community_view(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Render the read-only community status page for a wiki-posted check-in.

        Only check-ins that already posted to a community wiki (``wiki_notified_at``
        set) are visible, and only by UUID - slugs are unique per-profile, not
        globally, so a slug can't safely identify another user's check-in. The
        page deliberately omits the trip plan, contacts, and chat: the owner
        opted into telling the community *that* they're overdue and where, not
        into sharing their whole check-in.

        Args:
            request: Incoming HTTP request.
            checkin_slug: The URL identifier - must be the check-in's UUID.

        Returns:
            Rendered community status page.

        Raises:
            Http404: If the identifier isn't a UUID of a wiki-posted check-in.
        """
        try:
            checkin = get_object_or_404(SafetyCheckin.objects.select_related("profile"), uuid=checkin_slug, wiki_notified_at__isnull=False)
        except ValidationError as exc:
            raise Http404 from exc
        return render(
            request,
            "dashboard/pages/safety/community_status.html",
            {
                "checkin": checkin,
                "wiki": find_community_wiki(checkin.destination_latitude, checkin.destination_longitude),
                "map_attribution": _MAP_ATTRIBUTION,
            },
        )

    def post(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Autosave an edit, or cancel the check-in.

        Trip plan and destination stay editable at any time - editing either after contacts
        have been notified (``checkin.contacts_locked``) re-notifies them, debounced by
        ``notify_contacts_of_update``'s cooldown. Title, message, and the contact list are only
        applied while unlocked; the frontend never submits them once locked (their inputs are
        disabled), but they're re-checked here too in case a request bypasses the UI.

        Args:
            request: Incoming HTTP request. ``action=cancel`` cancels the check-in; otherwise
                whichever fields are present are saved.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            For an XHR autosave request, a JSON summary (including any ``warnings`` about
            fields that couldn't be changed, and a freshly-rendered contact-picker fragment).
            Otherwise, a redirect back to the check-in detail page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)

        if request.POST.get("action") == "cancel":
            from urbanlens.dashboard.services.safety import cancel_checkin

            cancel_checkin(checkin)
            return redirect("safety.home")

        is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        warnings: list[str] = []
        plan_or_map_changed = False

        lat = request.POST.get("destination_latitude") or None
        lng = request.POST.get("destination_longitude") or None
        new_lat = float(lat) if lat else None
        new_lng = float(lng) if lng else None
        old_lat = float(checkin.destination_latitude) if checkin.destination_latitude is not None else None
        old_lng = float(checkin.destination_longitude) if checkin.destination_longitude is not None else None

        new_plan = request.POST.get("plan_details", checkin.plan_details).strip()
        if new_plan != checkin.plan_details:
            checkin.plan_details = new_plan
            plan_or_map_changed = True
        if new_lat != old_lat or new_lng != old_lng:
            checkin.destination_latitude = new_lat
            checkin.destination_longitude = new_lng
            plan_or_map_changed = True

        update_fields = ["plan_details", "destination_latitude", "destination_longitude", "updated"]

        if checkin.contacts_locked:
            submitted_title = request.POST.get("title")
            if submitted_title is not None and submitted_title.strip() != checkin.title:
                warnings.append("Title is locked and can't be changed once contacts have been notified.")
        else:
            checkin.title = request.POST.get("title", checkin.title).strip() or checkin.title
            update_fields.append("title")

        if checkin.notifications_locked:
            existing_contacts = list(checkin.contacts.all())
            submitted_message = request.POST.get("contact_message")
            if submitted_message is not None and submitted_message.strip() != checkin.contact_message:
                warnings.append("Message is locked and can't be changed once contacts have been notified or you've checked in.")
            submitted_ids = set(request.POST.getlist("contact_profile_ids"))
            submitted_emails = {e.strip().lower() for e in request.POST.getlist("contact_emails") if e.strip()}
            current_ids = {str(c.contact_profile_id) for c in existing_contacts if c.contact_profile_id}
            current_emails = {c.email.lower() for c in existing_contacts if c.email}
            if submitted_ids != current_ids or submitted_emails != current_emails:
                warnings.append("Contacts are locked and can't be changed once they've been notified or you've checked in.")
            if "notify_community_wiki" in request.POST and not checkin.notify_community_wiki:
                warnings.append("Community wiki notification is locked and can't be changed once contacts have been notified or you've checked in.")
        else:
            checkin.contact_message = request.POST.get("contact_message", checkin.contact_message).strip()
            # Absent means unchecked - either the box was cleared or the destination has no
            # community wiki (the toggle isn't rendered at all then), which disables it too.
            checkin.notify_community_wiki = "notify_community_wiki" in request.POST
            update_fields += ["contact_message", "notify_community_wiki"]

            allowed, rejected = validate_notifiable_contacts(profile, _parse_contacts_from_post(request, profile), checkin=checkin)
            set_checkin_contacts(checkin, allowed)
            warnings.extend(rejected)

        checkin.save(update_fields=update_fields)

        if plan_or_map_changed and checkin.contacts_locked:
            notify_contacts_of_update(checkin, "updated their trip plan or destination")

        if is_xhr:
            contacts = list(checkin.contacts.all())
            contacts_html = render_to_string(
                "dashboard/partials/safety/_contact_picker.html",
                {
                    "contacts": [(c.contact_profile, c.email, c.name) for c in contacts],
                    "connections": get_connections(profile),
                    "contact_status": _contact_status_map(checkin, contacts),
                    "locked": checkin.notifications_locked,
                    "collapsible": True,
                },
                request=request,
            )
            return JsonResponse({"ok": True, "warnings": warnings, "contacts_html": contacts_html})
        for warning in warnings:
            messages.warning(request, warning)
        return redirect("safety.checkin.detail", checkin_slug=checkin.slug)


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


class SafetyCheckinDeleteView(LoginRequiredMixin, View):
    """Permanently delete a safety check-in (owner-only).

    If the check-in hasn't been resolved yet, it's routed through the normal
    self-check-in flow first (``services.safety.check_in``) so any side effects
    that flow carries - today, resolving the check-in and raising a visit
    suggestion; it does not itself email already-notified contacts - happen
    before the row disappears, rather than silently vanishing out from under
    an in-progress escalation.

    POST /safety/<slug:checkin_slug>/delete/
    """

    def post(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Resolve (if needed) and delete the check-in, then redirect home.

        Args:
            request: Incoming HTTP request.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            Redirect to the safety home page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        if not checkin.is_resolved:
            check_in(checkin, profile)
        stash_for_undo("safety_checkin", [checkin], profile)
        checkin.delete()
        messages.success(request, "Check-in deleted. You can undo this from Settings → Undo History within 7 days.")
        return redirect("safety.home")


class SafetyCheckinCheckInView(LoginRequiredMixin, View):
    """Self check-in link target, from the reminder email/notification.

    GET  /safety/<slug:checkin_slug>/checkin/ - confirmation page.
    POST /safety/<slug:checkin_slug>/checkin/ - actually check in.
    """

    def get(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Render a confirmation page for checking in.

        Args:
            request: Incoming HTTP request.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            Rendered confirmation page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        return render(request, "dashboard/pages/safety/checkin_confirm.html", {"checkin": checkin})

    def post(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Check in and redirect to the check-in detail page.

        Args:
            request: Incoming HTTP request.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            Redirect to the check-in detail page.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        if not checkin.is_resolved:
            check_in(checkin, profile)
        return redirect("safety.checkin.detail", checkin_slug=checkin.slug)


class SafetyCheckinWikiOptionView(LoginRequiredMixin, View):
    """HTMX fragment: the "also notify the community wiki" toggle for a destination point.

    Re-fetched whenever the destination marker moves on the create/detail forms,
    so the toggle only ever shows when the picked point is actually covered by an
    existing community wiki.

    GET /safety/wiki-option/?destination_latitude=..&destination_longitude=..
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        """Render the toggle partial - empty when no wiki covers the destination.

        Args:
            request: Incoming HTTP request. Reads ``destination_latitude``/
                ``destination_longitude`` and the current ``notify_community_wiki``
                checkbox state (included so moving the pin preserves the choice).

        Returns:
            Rendered toggle fragment.
        """
        lat: float | None
        lng: float | None
        try:
            lat = float(request.GET.get("destination_latitude", ""))
            lng = float(request.GET.get("destination_longitude", ""))
        except ValueError:
            lat = lng = None
        wiki = find_community_wiki(lat, lng)
        last_wiki_edit, wiki_editor_count = wiki_notify_stats(wiki) if wiki else (None, 0)
        return render(
            request,
            "dashboard/partials/safety/_wiki_notify_toggle.html",
            {
                "wiki": wiki,
                "checked": "notify_community_wiki" in request.GET,
                "last_wiki_edit": last_wiki_edit,
                "wiki_editor_count": wiki_editor_count,
            },
        )


def _render_attached_maps(request: HttpRequest, checkin: SafetyCheckin) -> str:
    """Render the "attached maps" list partial for a check-in.

    Args:
        request: Incoming HTTP request (needed for template context processors).
        checkin: The check-in whose ``markup_maps`` should be rendered.

    Returns:
        Rendered HTML for ``_attached_maps.html``.
    """
    return render_to_string(
        "dashboard/partials/safety/_attached_maps.html",
        {"checkin": checkin, "attached_maps": checkin.markup_maps.all()},
        request=request,
    )


class SafetyCheckinMapPickerView(LoginRequiredMixin, View):
    """HTMX dialog fragment: browse the profile's own maps to attach to a check-in.

    Excludes the check-in's primary (drawn) route map and any map already attached,
    so the list only ever offers maps that attaching would actually add.

    GET /safety/<slug:checkin_slug>/maps/picker/?q=<title filter>
    """

    def get(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Render the picker dialog fragment.

        Args:
            request: Incoming HTTP request. Reads the optional ``q`` title filter.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            Rendered picker fragment.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        excluded_ids = list(checkin.markup_maps.values_list("pk", flat=True))
        if checkin.markup_map_id:
            excluded_ids.append(checkin.markup_map_id)
        candidates = MarkupMap.objects.filter(profile=profile).exclude(pk__in=excluded_ids)
        query = request.GET.get("q", "").strip()
        if query:
            candidates = candidates.filter(title__icontains=query)
        return render(
            request,
            "dashboard/partials/safety/_map_picker.html",
            {"checkin": checkin, "candidates": candidates[:25], "query": query},
        )


class SafetyCheckinMapAttachView(LoginRequiredMixin, View):
    """Attach one of the profile's own existing maps to a check-in as a reference map.

    POST /safety/<slug:checkin_slug>/maps/attach/
    """

    def post(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Attach the given map and return the refreshed attached-maps list.

        Args:
            request: Incoming HTTP request. Reads ``map_uuid``.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            Rendered attached-maps partial, or a 400 if ``map_uuid`` doesn't
            resolve to a map owned by the caller.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        map_uuid = request.POST.get("map_uuid", "").strip()
        try:
            markup_map = MarkupMap.objects.get(uuid=map_uuid, profile=profile)
        except (MarkupMap.DoesNotExist, ValidationError, ValueError):
            return HttpResponseBadRequest("Invalid map.")
        checkin.markup_maps.add(markup_map)
        return HttpResponse(_render_attached_maps(request, checkin))


class SafetyCheckinMapDetachView(LoginRequiredMixin, View):
    """Detach a reference map from a check-in without deleting the map itself.

    POST /safety/<slug:checkin_slug>/maps/<uuid:map_uuid>/detach/
    """

    def post(self, request: HttpRequest, checkin_slug: str, map_uuid: str) -> HttpResponse:
        """Remove the map from the check-in's ``markup_maps`` and return the refreshed list.

        Args:
            request: Incoming HTTP request.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.
            map_uuid: UUID of the map to detach.

        Returns:
            Rendered attached-maps partial.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        checkin.markup_maps.remove(*checkin.markup_maps.filter(uuid=map_uuid))
        return HttpResponse(_render_attached_maps(request, checkin))


class SafetyGalleryView(LoginRequiredMixin, View):
    """Photo gallery panel for the safety check-in detail page (owner-only).

    Mirrors ``PinGalleryView``/``WikiGalleryView`` (``controllers/image_gallery.py``)
    so the check-in detail page can reuse the same gallery partial/JS - lightbox,
    drag-drop upload, captions - instead of the plain grid it had before.

    GET  /safety/<slug:checkin_slug>/gallery/ - HTML gallery partial.
    POST /safety/<slug:checkin_slug>/gallery/ - upload a photo.
    """

    def _get_context(self, request: HttpRequest, checkin_slug: str) -> dict:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        images = Image.objects.filter(safety_checkin=checkin).select_related("profile").order_by("-created")
        page_obj = get_page(request, images, _GALLERY_PAGE_SIZE)
        return {
            "checkin": checkin,
            "images": page_obj.object_list,
            "page_obj": page_obj,
            "profile": profile,
            "context_type": "safety",
        }

    def get(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Render the gallery partial.

        Args:
            request: Incoming HTTP request.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            Rendered gallery partial.
        """
        return render(request, "dashboard/partials/pins/_photo_gallery.html", self._get_context(request, checkin_slug))

    def post(self, request: HttpRequest, checkin_slug: str) -> HttpResponse:
        """Attach an uploaded photo to the check-in.

        Args:
            request: Incoming HTTP request. Reads the ``image`` file and optional ``caption``.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.

        Returns:
            JSON describing the new image, or a 400 if no file was given.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)
        from urbanlens.dashboard.services.images import compute_checksum

        checksum = compute_checksum(image_file)
        if Image.objects.filter(safety_checkin=checkin, checksum=checksum).exists():
            return JsonResponse({"error": "That photo is already on this check-in."}, status=409)
        from urbanlens.dashboard.services.storage import quota_error_for_upload

        quota_error = quota_error_for_upload(profile, image_file.size)
        if quota_error:
            return JsonResponse({"error": quota_error}, status=413)
        img = Image.objects.create(
            image=image_file,
            safety_checkin=checkin,
            location=checkin.destination_location,
            profile=profile,
            caption=request.POST.get("caption", "").strip() or None,
            checksum=checksum,
            file_size=image_file.size,
        )
        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse(image_to_gallery_json(img, request, profile), status=201)


class SafetyImageView(LoginRequiredMixin, View):
    """Reposition or delete a single photo on a safety check-in (owner-only).

    POST   /safety/<slug:checkin_slug>/gallery/<int:image_id>/ - update lat/lng.
    DELETE /safety/<slug:checkin_slug>/gallery/<int:image_id>/
    """

    def _get_image(self, request: HttpRequest, checkin_slug: str, image_id: int) -> Image:
        profile, _ = Profile.objects.get_or_create(user=request.user)
        checkin = _get_checkin_by_slug(profile, checkin_slug)
        return get_object_or_404(Image, pk=image_id, safety_checkin=checkin, profile=profile)

    def post(self, request: HttpRequest, checkin_slug: str, image_id: int) -> HttpResponse:
        """Update lat/lng when the user drags the photo marker on the map.

        Args:
            request: Incoming HTTP request with a JSON body (``latitude``/``longitude``).
            checkin_slug: Slug (or, for older links, UUID) of the check-in.
            image_id: The image being repositioned.

        Returns:
            JSON with the saved coordinates, or a 400 on bad input.
        """
        img = self._get_image(request, checkin_slug, image_id)
        try:
            data = json.loads(request.body)
            img.latitude = Decimal(str(data["latitude"]))
            img.longitude = Decimal(str(data["longitude"]))
            img.save(update_fields=["latitude", "longitude", "updated"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Failed to update image %s on checkin %s: %s", image_id, checkin_slug, exc)
            return JsonResponse({"error": "Invalid request data."}, status=400)
        return JsonResponse({"latitude": float(img.latitude), "longitude": float(img.longitude)})

    def delete(self, request: HttpRequest, checkin_slug: str, image_id: int) -> HttpResponse:
        """Delete a photo from the check-in.

        Args:
            request: Incoming HTTP request.
            checkin_slug: Slug (or, for older links, UUID) of the check-in.
            image_id: The image being deleted.

        Returns:
            204 on success.
        """
        img = self._get_image(request, checkin_slug, image_id)
        img.image.delete(save=False)
        img.delete()
        return HttpResponse(status=204)


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
                "map_attribution": _MAP_ATTRIBUTION,
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


class SafetyContactOptOutView(View):
    """Let a contact stop receiving certain safety check-in notifications (token-gated, no login).

    GET renders a confirmation page rather than performing the opt-out directly - a bare
    GET link is exactly what an email client's link-scanner prefetches, which would otherwise
    silently unsubscribe a real emergency contact. The opt-out only actually happens on the
    POST from that confirmation page's button, mirroring the mark-safe flow's own confirm step.

    GET  /safety/contact/<uuid:token>/opt-out/<str:scope>/
    POST /safety/contact/<uuid:token>/opt-out/<str:scope>/
    """

    def get(self, request: HttpRequest, token: str, scope: str) -> HttpResponse:
        """Render the opt-out confirmation page.

        Args:
            request: Incoming HTTP request.
            token: The contact's magic-link token.
            scope: One of ``SafetyContactOptOutScope`` values.

        Returns:
            Rendered confirmation page, or 404 if the token or scope is invalid.
        """
        if SafetyContactOptOutScope.invalid(scope):
            raise Http404
        contact = get_object_or_404(SafetyCheckinContact.objects.select_related("checkin", "checkin__profile"), token=token)
        return render(
            request,
            "dashboard/pages/safety/contact_optout_confirm.html",
            {
                "checkin": contact.checkin,
                "contact": contact,
                "scope": scope,
                "scope_label": SafetyContactOptOutScope(scope).label,
            },
        )

    def post(self, request: HttpRequest, token: str, scope: str) -> HttpResponse:
        """Record the opt-out and redirect back to the contact portal.

        Args:
            request: Incoming HTTP request.
            token: The contact's magic-link token.
            scope: One of ``SafetyContactOptOutScope`` values.

        Returns:
            Redirect back to the contact portal, or 404 if the token or scope is invalid.
        """
        if SafetyContactOptOutScope.invalid(scope):
            raise Http404
        contact = get_object_or_404(SafetyCheckinContact, token=token)
        record_contact_opt_out(contact, SafetyContactOptOutScope(scope))
        messages.success(request, "You won't receive further notifications about this trip.")
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
            Rendered message list partial, or a plain-text 400 if the message
            was rejected (e.g. blank or too long) - the chat panel's JS reads
            this body verbatim to tell the sender why it didn't send.
        """
        checkin, contact = self._resolve(request, checkin_uuid, token)
        body = request.POST.get("body", "").strip()
        if body:
            try:
                create_chat_message(checkin, user=request.user, contact=contact, body=body)
            except ValueError as exc:
                # create_chat_message only raises ValueError with a fixed, developer-authored
                # message (blank/too-long body) - never a stack trace or sensitive data.
                logger.info("Safety chat HTTP fallback rejected message on checkin %s: %s", checkin.uuid, exc)
                return HttpResponseBadRequest(str(exc))  # lgtm[py/stack-trace-exposure]
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
