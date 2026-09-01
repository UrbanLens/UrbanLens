"""Pin inline-edit and personal notes controllers."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.pins.pin_edit import SECURITY_EDIT_FIELDS, apply_pin_edits
from urbanlens.dashboard.services.pins.pin_subresources import create_pin_note, delete_pin_note

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def _pin_for_user(pin_slug, request) -> Pin | HttpResponse:
    """Return the pin if it belongs to the requesting user.

    Returns 403 when the requester has no authenticated profile at all. Any
    other user's pin - whether it exists or not - returns 404: the lookup is
    scoped to the requester's own profile, so a pin owned by someone else is
    indistinguishable from a nonexistent one and its existence is never leaked.
    """
    if not request.user.is_authenticated or not request.user.profile:
        return HttpResponse("Forbidden", status=403)
    try:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile__user"), slug=pin_slug, profile=request.user.profile)
    except Http404:
        return HttpResponse(status=404)
    return pin


def _pin_version(pin: Pin) -> str:
    """Return an opaque version token for the pin's last-saved state.

    Clients echo this back on quick-edit requests (star clicks) so the server can tell
    whether anything else changed since that client last rendered the pin - see
    PinEditView.post for how this drives the minimal vs. full-resync response.
    """
    return str(int(pin.updated.timestamp())) if pin.updated else ""


# Metadata for the four single-field 1-5 star-rating widgets, shared between the full
# overview render and the minimal single-field response in PinEditView.post.
STAT_FIELD_META = {
    "danger": {
        "label": "Danger",
        "help": "How hazardous this site feels - structural risks, environmental hazards, or unsafe conditions (1 = low, 5 = extreme).",
        "modifier": "danger",
        "wide": True,
    },
    "priority": {
        "label": "Priority",
        "help": "How urgently you want to visit this pin (1 = low, 5 = must visit soon).",
        "modifier": "priority",
        "wide": False,
    },
    "rating": {
        "label": "Rating",
        "help": "Your quality rating for this location.",
        "modifier": "",
        "wide": False,
    },
    "vulnerability": {
        "label": "Vulnerability",
        "help": "How at-risk or fragile this site feels - useful for planning and sharing responsibly.",
        "modifier": "vulnerability",
        "wide": True,
    },
}


def _stat_item_context(pin: Pin, field: str) -> dict:
    return {"pin": pin, "field": field, "client_version": _pin_version(pin), **STAT_FIELD_META[field]}


def _overview_context(pin: Pin) -> dict:
    from urbanlens.dashboard.models.labels.model import COLOR_CHOICES
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import PinType

    detail_pin_icon_choices = [
        ("place", "Place"),
        ("business", "Building"),
        ("door_front", "Entrance"),
        ("star", "Star"),
        ("warning", "Warning"),
        ("info", "Info"),
        ("camera_alt", "Camera"),
        ("local_parking", "Parking"),
        ("stairs", "Stairs"),
        ("elevator", "Elevator"),
        ("exit_to_app", "Exit"),
        ("lock", "Lock"),
        ("construction", "Construction"),
        ("emergency", "Emergency"),
    ]

    # Every wiki this pin is genuinely associated with - the pin's own, any
    # genuinely competing same-coordinate property, and any earned ancestor
    # in a split-derived family. This used to be "every Location covering
    # this point", which on a campus meant every building on it - all the
    # same place, none of them a choice.
    from urbanlens.dashboard.services.places.ambiguity import linked_wiki_locations

    linked_locations = linked_wiki_locations(pin, pin.profile)

    from urbanlens.dashboard.services.ai.link_extraction import ai_extract_button_context

    return {
        "pin": pin,
        "client_version": _pin_version(pin),
        "pin_type_choices": PinType.choices,
        "all_categories": Label.objects.categories().ordered(),
        "detail_pin_icon_choices": detail_pin_icon_choices,
        "color_choices": COLOR_CHOICES,
        "security_level_choices": SecurityLevel.choices,
        "linked_wiki_locations": linked_locations,
        **ai_extract_button_context(pin.profile.user, pin.profile, pin),
        "pin_security_values": [
            ("fences", "Fences", pin.fences),
            ("alarms", "Alarms", pin.alarms),
            ("cameras", "Cameras", pin.cameras),
            ("security", "Security", pin.security),
            ("signs", "Signs", pin.signs),
            ("vps", "VPS", pin.vps),
            ("plywood", "Plywood", pin.plywood),
            ("locked", "Locked", pin.locked),
        ],
    }


def _pin_hero_oob(request, pin: Pin, *, linked_wiki_locations: list[Location]) -> str:
    """Render the Private Pin page hero as an out-of-band HTMX swap.

    The hero (with its Community Wiki box) lives in base.html's
    ``{% block hero %}`` (see ``pages/location/index.html``), outside
    ``#pin-overview`` - so ``PinOverviewView``'s slug backfill (see below)
    would otherwise leave an already-loaded page's hero permanently stuck
    showing "no wiki" until a full reload, even though the location now has
    a slug and could show the create-wiki button.
    """
    from urbanlens.dashboard.services.places.scope import scope_badge

    cover_image = pin.cover_photo.image if pin.cover_photo and pin.cover_photo.image else None
    return render_to_string(
        request=request,
        template_name="dashboard/partials/ui/_page_hero.html",
        context={
            "pin": pin,
            "id": "pin-detail-hero",
            "oob": True,
            "body_template": "dashboard/partials/pins/_pin_detail_hero_body.html",
            "back_url": reverse("map.view"),
            "back_label": "Map",
            "modifier": "top",
            "hero_image_url": cover_image.url if cover_image else None,
            "hero_cover_key": "pin",
            "linked_wiki_locations": linked_wiki_locations,
            # The hero carries the parcel/building badge, so an out-of-band
            # swap has to rebuild it too or organising a property would blank
            # the badge until the next full page load.
            **scope_badge(pin),
        },
    )


class PinOverviewView(LoginRequiredMixin, View):
    """Render the swappable pin overview partial (title + details card).

    GET /map/pin/<uuid>/overview/
    """

    def get(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        pin.backfill_wiki_link_slugs()
        # Address and place-name backfills both happen in the background:
        # neither may block this request on a live Google call. The rendered
        # partial reads whatever the Location row / place-name cache already
        # hold, and the next render of this Location (by any pin/user sharing
        # its coordinates) finds the backfilled data instead of it staying
        # permanently empty. (The address half used to be a synchronous
        # geocoding call right here - the last inline external call on this
        # page's render path.)
        if pin.location and pin.profile.external_apis_enabled:
            from urbanlens.dashboard.services.core.celery import safely_enqueue_task
            from urbanlens.dashboard.tasks import backfill_location_address, resolve_location_place_name

            if not pin.location.route:
                safely_enqueue_task(backfill_location_address, pin.location_id)
            if not pin.location.cached_place_name:
                safely_enqueue_task(resolve_location_place_name, pin.location_id)
        overview_context = _overview_context(pin)
        overview_html = render_to_string(request=request, template_name="dashboard/partials/pins/pin_overview_partial.html", context=overview_context)
        hero_html = _pin_hero_oob(request, pin, linked_wiki_locations=overview_context["linked_wiki_locations"])
        return HttpResponse(overview_html + hero_html)


class PinEditView(LoginRequiredMixin, View):
    """Update editable pin fields.

    POST /map/pin/<uuid>/edit/
    Re-renders the pin overview partial on success.
    """

    def post(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        # Snapshot of what the client believed the pin's state was when it sent this
        # request, captured before any of this request's own changes are applied. Used
        # below to detect whether another tab/session changed the pin in the meantime.
        client_version = body.get("client_version")
        pre_save_version = _pin_version(pin)

        from datetime import date, datetime

        # Star-rating widgets and other quick-edit controls submit only the one
        # field they changed, so anything absent from the body must be left
        # alone rather than rewritten with its current value. `edits` therefore
        # collects *only* what this request actually submitted, and
        # ``services.pins.pin_edit.apply_pin_edits`` writes exactly that much.
        edits: dict[str, object] = {}

        if "name" in body:
            edits["name"] = body.get("name")
        if "description" in body:
            description = (body.get("description") or "").strip() or None
            length_error = text_length_error(description, MAX_PIN_DESCRIPTION_LENGTH, "Description")
            if length_error:
                return HttpResponse(length_error, status=400)
            edits["description"] = description

        # A browser form is a lenient caller: an out-of-range or unparseable
        # value means a stale/hand-edited control, and dropping that one field
        # is friendlier than failing the whole dialog. (The JSON API is strict
        # instead and answers 400 - see external_api.serializers.PinUpdateSerializer.)
        for stat_field in ("priority", "vulnerability", "danger"):
            raw = body.get(stat_field)
            if raw is None or not str(raw).strip():
                continue
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= parsed <= 5:
                edits[stat_field] = parsed

        # clear_rating distinguishes "explicitly submitted 0" (delete the
        # Review row) from "field untouched, pin.rating just defaults to 0
        # because no Review exists yet" (nothing to do) - collapsing both
        # into rating=0 would either silently no-op a real clear request, or
        # issue a pointless delete query on every unrelated quick-edit.
        rating_raw = body.get("rating")
        clear_rating = False
        try:
            if rating_raw is not None and str(rating_raw).strip():
                rating = int(rating_raw)
                if not (0 <= rating <= 5):
                    rating = pin.rating
                elif rating == 0:
                    clear_rating = True
            else:
                rating = pin.rating
        except (TypeError, ValueError):
            rating = pin.rating

        last_visited_raw = (body.get("last_visited") or "").strip() or None
        if last_visited_raw:
            last_visited = None
            for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                try:
                    last_visited = datetime.strptime(last_visited_raw, fmt)
                    break
                except ValueError:
                    continue
            if last_visited:
                today = timezone.localdate()
                min_date = date(today.year - 100, today.month, today.day)
                lv_date = last_visited.date()
                if lv_date > today:
                    return HttpResponse("Last visited date must be in the past.", status=400)
                if lv_date < min_date:
                    return HttpResponse("Last visited date must be within the last 100 years.", status=400)
                edits["last_visited"] = last_visited

        # Security indicators. Anything not a recognized SecurityLevel is
        # treated as "not submitted" - same leniency as the stat fields above.
        valid_security = {value for value, _label in SecurityLevel.choices}
        for security_field in SECURITY_EDIT_FIELDS:
            raw = body.get(security_field, "")
            if raw in valid_security:
                edits[security_field] = raw

        # Abandonment dates. Present-but-unparseable clears the field, matching
        # the date input's own "clear" behavior (it posts an empty string).
        def _parse_date(raw: str) -> date | None:
            raw = (raw or "").strip()
            if not raw:
                return None
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return None

        for date_field in ("date_built", "date_abandoned", "date_last_active"):
            if date_field in body:
                edits[date_field] = _parse_date(body.get(date_field, ""))

        valid_types = {value for value, _label in PinType.choices}
        if body.get("pin_type") in valid_types:
            edits["pin_type"] = body["pin_type"]

        apply_pin_edits(pin, edits)

        # rating lives on the Review model (one review per user per pin)
        if rating and 1 <= rating <= 5:
            Review.objects.update_or_create(
                profile=request.user.profile,
                pin=pin,
                defaults={"rating": rating},
            )
        elif clear_rating:
            Review.objects.for_pair(request.user.profile, pin).delete()

        # Category update: only runs when the field was explicitly submitted (partial requests preserve existing)
        if "categories" in body:
            category_raw = (body.get("categories") or "").strip()
            names = [n.strip().lower() for n in category_raw.split(",") if n.strip()]
            seen_names: set[str] = set()
            pin.labels.remove(*pin.labels.filter(kind=KIND_CATEGORY))
            for name in names:
                if name in seen_names:
                    continue
                seen_names.add(name)
                cat = Label.objects.filter(name__iexact=name, kind=KIND_CATEGORY, profile=pin.profile).first()
                if cat is None:
                    cat, _ = Label.objects.get_or_create(
                        name=name,
                        kind=KIND_CATEGORY,
                        profile=pin.profile,
                    )
                pin.labels.add(cat)

        # Reload from DB so all properties reflect saved state
        pin.refresh_from_db()

        # Quick-edit widgets (star ratings) submit exactly one field at a time. When the
        # client's last-known version still matches what was in the DB before this save,
        # nothing else has drifted, so we only need to send back the one fragment that
        # changed - this is the common case and keeps these frequent requests tiny.
        # If something else changed (e.g. a different tab edited the name), fall back to
        # a full resync: the small fragment still satisfies the primary hx-target swap,
        # and an out-of-band re-render of the whole card brings everything else current.
        submitted_fields = set(body.keys()) - {"client_version"}
        if len(submitted_fields) == 1 and submitted_fields <= set(STAT_FIELD_META):
            field = next(iter(submitted_fields))
            fragment = render(request, "dashboard/partials/pins/_pin_stat_rating_item.html", _stat_item_context(pin, field))
            if client_version is not None and client_version == pre_save_version:
                return fragment
            oob_context = {**_overview_context(pin), "oob": True}
            oob = render(request, "dashboard/partials/pins/pin_overview_partial.html", oob_context)
            return HttpResponse(fragment.content + oob.content)

        return render(request, "dashboard/partials/pins/pin_overview_partial.html", _overview_context(pin))


class PinNotesView(LoginRequiredMixin, View):
    """Personal notes panel for a pin.

    GET  /map/pin/<uuid>/notes/  → render panel
    POST /map/pin/<uuid>/notes/  → add note, re-render panel
    """

    def get(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        notes = pin.notes.order_by("-created")
        return render(request, "dashboard/partials/pins/pin_notes_panel.html", {"pin": pin, "notes": notes})

    def post(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        try:
            create_pin_note(pin, text=(body.get("text") or ""))
        except ValueError:
            return HttpResponse("Note text is required.", status=400)
        notes = pin.notes.order_by("-created")
        return render(request, "dashboard/partials/pins/pin_notes_panel.html", {"pin": pin, "notes": notes})


class PinNoteDeleteView(LoginRequiredMixin, View):
    """Delete a single personal note.

    DELETE /map/pin/<uuid>/notes/<int:note_id>/delete/
    """

    def delete(self, request, pin_slug, note_id):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        note = get_object_or_404(PinNote, id=note_id, pin=pin)
        delete_pin_note(note)
        return HttpResponse("", status=200)


class PinDetachChildView(LoginRequiredMixin, View):
    """Promote a child (sub) pin back to a top-level pin of its own.

    POST /map/pin/<pin_slug>/detach-parent/

    Returns 200 with an ``HX-Refresh`` header so the page re-renders as a
    root pin, or 400 with a plain-text reason when detaching is impossible
    (two top-level pins can't share one Location per profile).
    """

    def post(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        if pin.parent_pin_id is None:
            return HttpResponse("This pin is already a top-level pin.", status=400)
        conflict = Pin.objects.filter(profile=pin.profile, location_id=pin.location_id, parent_pin__isnull=True).exclude(pk=pin.pk).exists()
        if conflict:
            return HttpResponse("You already have a top-level pin at this exact location. Move this child pin slightly before detaching it.", status=400)
        logger.info("User %s detached child pin %s from parent %s", request.user.id, pin.id, pin.parent_pin_id)
        pin.parent_pin = None
        pin.save(update_fields=["parent_pin", "updated"])
        response = HttpResponse("", status=200)
        response["HX-Refresh"] = "true"
        return response


class PinPromoteChildrenView(LoginRequiredMixin, View):
    """Promote a pin's direct children up one level, from the map popup.

    POST /map/pin/<pin_slug>/promote-children/

    Children move to this pin's own parent (or become top-level pins if this
    pin has none); the pin itself is untouched. Returns JSON so the map popup
    can update in place without a full page reload.
    """

    def post(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        child_count = Pin.objects.filter(parent_pin=pin).count()
        if not child_count:
            return JsonResponse({"error": "This pin has no child pins to promote."}, status=400)
        promoted = pin.promote_children()
        logger.info("User %s promoted %s child pin(s) of pin %s", request.user.id, promoted, pin.id)
        return JsonResponse({"ok": True, "promoted": promoted})


class PinSwapParentView(LoginRequiredMixin, View):
    """Swap a child pin with its parent - the child becomes the parent, and vice versa.

    POST /map/pin/<pin_slug>/swap-parent/

    ``pin_slug`` is the child pin being promoted. Returns JSON with both
    pins' slugs so the caller can redirect/re-render appropriately (the
    detail page a user is currently viewing may no longer be the "top-level"
    one after this).
    """

    def post(self, request, pin_slug):
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        try:
            old_parent = pin.swap_with_parent()
        except ValueError as exc:
            # swap_with_parent() raises one of exactly two developer-authored
            # literals; match on it rather than echoing exc so a future raise
            # site added there can't smuggle unsafe text into this response.
            if str(exc) == "This pin has no parent to swap with.":
                return JsonResponse({"error": "This pin has no parent to swap with."}, status=400)
            return JsonResponse({"error": "Can't complete the swap - you already have a top-level pin at this pin's own location."}, status=400)
        logger.info("User %s swapped pin %s with its parent %s", request.user.id, pin.id, old_parent.id)
        return JsonResponse({"ok": True, "new_parent_slug": pin.slug, "new_child_slug": old_parent.slug})


class PinRelinkView(LoginRequiredMixin, View):
    """Link a pin to a different Location.

    GET  /map/pin/<uuid>/link/               → HTML picker listing all overlapping Locations
    POST /map/pin/<uuid>/link/<loc_uuid>/    → Relink: switches the pin to the given Location

    Each route carries exactly one of those verbs; the other is refused with a
    405 rather than falling through to a handler written for its sibling.
    """

    def get(self, request, pin_slug, location_slug=None):
        """Return an HTMX partial listing every Location that covers this pin's point.

        This view backs two routes, and only ``pin.link`` has a meaningful GET:
        it renders the picker. ``pin.link.to`` already names the location, so a
        GET there has nothing to choose and is refused. The parameter was absent
        from this signature entirely, which made that request a ``TypeError``
        before any code ran - a guaranteed 500 on a route reachable by anyone
        who edits the URL. Same shape as ``saved_filters.new`` (audit chunk 552):
        one view, two routes, a signature that fits only one of them.

        Args:
            request: The HTTP request.
            pin_slug: UUID of the pin.
            location_slug: Present only on ``pin.link.to``, where GET is refused.

        Returns:
            Rendered HTML partial with location choices, or 405 when a location
            is already named.
        """
        if location_slug is not None:
            return HttpResponseNotAllowed(["POST"])
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        from urbanlens.dashboard.services.places.ambiguity import competing_wiki_locations

        # The pin's current location plus any genuinely competing property.
        # Every other location covering this point describes the same place -
        # switching between them would change nothing a user can perceive.
        locations = [pin.location] if pin.location_id else []
        locations += [candidate for candidate in competing_wiki_locations(pin, pin.profile) if candidate.pk != pin.location_id]
        return render(
            request,
            "dashboard/partials/pins/pin_location_picker.html",
            {"pin": pin, "locations": locations},
        )

    def post(self, request, pin_slug, location_slug=None):
        """Relink the pin to a named Location, or merge it into an existing pin there.

        Args:
            request: The HTTP request.
            pin_slug: Slug (or uuid) of the pin.
            location_slug: Slug (or uuid) of the Location to link to. Absent
                only on ``pin.link``, which is GET-only.

        Returns:
            For the raw-fetch caller (map.html's location-conflict dialog,
            identified by ``X-Requested-With``): a JSON verdict. Otherwise
            (the HTMX-driven pin-location picker): the re-rendered pin
            overview partial. 405 when no location is named.
        """
        if location_slug is None:
            return HttpResponseNotAllowed(["GET"])
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result
        is_xhr = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.wiki.wiki_access import location_visible_to

        location = get_object_or_404(Location.objects.slug_or_uuid(location_slug))
        # Which Location a pin points at is not a neutral preference - it is what
        # confers access, since location_visible_to grants on an exact Location
        # match. Unchecked, relinking is a way to *earn* a community wiki rather
        # than discover one, and a Location's slug is its official_name, so the slug
        # of any notable place is guessable.
        #
        # A target qualifies two ways, matching the two things the UI actually
        # offers. Either the profile can already reach it (the picker and the wiki
        # page's switch button both offer only candidates filtered to accessible
        # domains), or it covers the pin's own coordinate - the map's
        # location-conflict dialog offers exactly those, and a place the user's own
        # pin sits inside is one they discovered by pinning it, so allowing it
        # discloses nothing they could not already derive. Both are checked against
        # the pin's own point, never against an arbitrary slug from the URL.
        if not (location.pk == pin.location_id or location_visible_to(location, pin.profile) or Location.objects.get_all_for_point(pin.effective_latitude, pin.effective_longitude).filter(pk=location.pk).exists()):
            raise Http404

        # A profile can only ever have one root pin per location
        # (db_pin_unique_location_per_profile) - if one already exists at the
        # location we are about to point at, reassigning `pin.location` would
        # collide with it. Merge into the existing pin instead (same
        # reparent-as-child mechanism as PinBulkMergeView) rather than failing
        # with an IntegrityError.
        existing = Pin.objects.filter(profile=pin.profile, location=location, parent_pin__isnull=True).exclude(pk=pin.pk).first()
        if existing is not None:
            if not pin.would_create_cycle(existing):
                pin.parent_pin = existing
                pin.save(update_fields=["parent_pin", "updated"])
                pin.refresh_from_db()
            if is_xhr:
                from django.urls import reverse

                return JsonResponse(
                    {
                        "merged": True,
                        "existing_pin_url": reverse("pin.details", kwargs={"pin_slug": existing.slug or str(existing.uuid)}),
                        "existing_pin_name": existing.effective_name,
                    },
                )
            return render(request, "dashboard/partials/pins/pin_overview_partial.html", _overview_context(pin))

        # Wikis are user-created only: link to the location's wiki when one
        # exists, otherwise leave the pin wiki-less until someone creates one.
        pin.location = location
        pin.wiki = Wiki.objects.get_for_location(location)
        pin.save(update_fields=["location", "wiki", "updated"])
        pin.refresh_from_db()

        if is_xhr:
            return JsonResponse({"merged": False})
        return render(request, "dashboard/partials/pins/pin_overview_partial.html", _overview_context(pin))
