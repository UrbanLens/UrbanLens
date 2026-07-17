"""Wiki controller - community-editable page for a shared place.

Routes are keyed by the Location slug (the stable URL token) but every view
operates on the :class:`~urbanlens.dashboard.models.wiki.model.Wiki` for that
Location. Wikis are user-created (from the pin detail page); these views 404
when the place has no wiki yet.
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.models.wiki_stat_vote import WikiStatField, WikiStatVote
from urbanlens.dashboard.services.text_limits import MAX_WIKI_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.undo.handlers.wiki import with_wiki_descendants
from urbanlens.dashboard.services.undo.service import stash_for_undo
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

logger = logging.getLogger(__name__)

_WIKI_SECURITY_FIELDS = ("fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked")

# Fields a community member may edit via "Suggest edits". Coordinates are not
# editable here: a Wiki's Location is fixed at creation and is not something
# a community edit may repoint.
_WIKI_EDITABLE_FIELDS = ("name", "description", *_WIKI_SECURITY_FIELDS, "date_abandoned", "date_last_active")

# Metadata for the four community stat votes (danger / vulnerability / priority /
# rating) shown on the wiki page - the shared-place equivalent of a pin's own
# STAT_FIELD_META (see controllers/pin_edit.py), reworded for a community voice
# since these are a composite of every contributing profile's vote, not one
# person's opinion.
WIKI_STAT_FIELD_META = {
    "danger": {
        "label": "Danger",
        "help": "How hazardous this site feels to explorers - structural risks, environmental hazards, or unsafe conditions (1 = low, 5 = extreme).",
        "modifier": "danger",
        "wide": True,
    },
    "priority": {
        "label": "Priority",
        "help": "How urgently the community feels this place deserves a visit (1 = low, 5 = must visit soon).",
        "modifier": "priority",
        "wide": False,
    },
    "rating": {
        "label": "Rating",
        "help": "The community's overall quality rating for this place.",
        "modifier": "",
        "wide": False,
    },
    "vulnerability": {
        "label": "Vulnerability",
        "help": "How at-risk or fragile this site feels to the community - useful for planning and sharing responsibly.",
        "modifier": "vulnerability",
        "wide": True,
    },
}


def _wiki_stat_context(wiki: Wiki, field: str, profile: Profile | None) -> dict:
    """Build the template context for one community stat item.

    Args:
        wiki: The wiki the vote/composite belongs to.
        field: One of :class:`WikiStatField`'s values.
        profile: The viewing profile, used to look up their own vote.

    Returns:
        Dict with the composite, the viewer's own vote, and that field's
        label/help/modifier/wide metadata.
    """
    return {
        "field": field,
        "my_vote": WikiStatVote.objects.my_vote(wiki, field, profile),
        "composite": WikiStatVote.objects.composite(wiki, field),
        **WIKI_STAT_FIELD_META[field],
    }


class LocationWikiView(LoginRequiredMixin, View):
    """Main wiki page for a place.

    GET  /location/<slug>/wiki/  → full wiki page
    """

    def get(self, request, location_slug):
        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        # First view by someone other than the creator retires their
        # self-service delete eligibility (see Wiki.can_be_deleted_by).
        if wiki.created_by_id and profile.id != wiki.created_by_id and not wiki.viewed_by_other:
            Wiki.objects.filter(pk=wiki.pk).update(viewed_by_other=True)
            wiki.viewed_by_other = True

        # Only count root pins (not detail pins), and count distinct users.
        # The exact count is never exposed - see services.community_counts.
        from urbanlens.dashboard.services.community_counts import approximate_pin_count

        root_pins = location.pins.filter(parent_pin__isnull=True)
        pin_count = root_pins.values("profile").distinct().count()
        pin_count_display = approximate_pin_count(wiki.pk, pin_count)
        first_pinned = root_pins.select_related("profile__user").order_by("created").first()

        # The requesting user's own pin for this location (used for the back-link).
        user_pin = location.pins.filter(profile=profile).first()

        # Other Locations whose bounding box also covers the user's pin point.
        # These are potential alternative associations the user may prefer.
        if user_pin:
            lat = user_pin.effective_latitude
            lng = user_pin.effective_longitude
            other_locations = Location.objects.within_bounding_box(float(lat), float(lng)).exclude(pk=location.pk).order_by("official_name") if lat is not None and lng is not None else Location.objects.none()
        else:
            other_locations = Location.objects.none()

        from urbanlens.dashboard.models.labels.model import COLOR_CHOICES
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

        show_wiki_cover_photo = bool(profile.show_wiki_cover_photos and wiki.cover_photo_id)
        wiki_cover_candidates: list[dict] = []
        if show_wiki_cover_photo:
            from urbanlens.dashboard.models.images.model import Image

            wiki_cover_candidates = [{"id": img.pk, "url": img.image.url} for img in Image.objects.filter(wiki=wiki).exclude(pk=wiki.cover_photo_id).order_by("-created")[:20] if img.image]

        return render(
            request,
            "dashboard/pages/location/wiki.html",
            {
                "wiki": wiki,
                "location": location,
                "profile": profile,
                "show_wiki_cover_photo": show_wiki_cover_photo,
                "wiki_cover_candidates": wiki_cover_candidates,
                "can_delete_wiki": wiki.can_be_deleted_by(profile),
                "wiki_comment_count": wiki.comments.count(),
                "pin_count_display": pin_count_display,
                "first_pinned": first_pinned,
                "wiki_stats": [_wiki_stat_context(wiki, field, profile) for field in WikiStatField.values],
                "user_pin": user_pin,
                "other_locations": other_locations,
                "page_name": "location-wiki",
                "pin_type_choices": PinType.choices,
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
                "markup_fill_color": profile.markup_fill_color,
                "markup_fill_opacity": profile.markup_fill_opacity,
                "markup_border_color": profile.markup_border_color,
                "markup_border_opacity": profile.markup_border_opacity,
                "security_level_choices": SecurityLevel.choices,
                "location_security_values": [
                    ("fences", "Fences", wiki.fences),
                    ("alarms", "Alarms", wiki.alarms),
                    ("cameras", "Cameras", wiki.cameras),
                    ("security", "Security", wiki.security),
                    ("signs", "Signs", wiki.signs),
                    ("vps", "VPS", wiki.vps),
                    ("plywood", "Plywood", wiki.plywood),
                    ("locked", "Locked", wiki.locked),
                ],
                "show_map_footer": True,
            },
        )


class LocationWikiDeleteView(LoginRequiredMixin, View):
    """Let a wiki's creator delete it, before anyone else has seen it.

    DELETE /location/<slug>/wiki/delete/

    Only available to the profile that created the wiki, and only while
    ``Wiki.viewed_by_other`` is still false - see ``Wiki.can_be_deleted_by``.
    Once eligible, deletes the wiki and its full child-wiki subtree (stashed
    for undo, same as detail-wiki deletion) and unlinks it from the pin.
    """

    def delete(self, request, location_slug):
        location, wiki, profile = resolve_visible_wiki(request, location_slug)

        if not wiki.can_be_deleted_by(profile):
            return JsonResponse({"error": "This wiki can no longer be deleted - it's already been viewed by someone else."}, status=403)

        user_pin = location.pins.filter(profile=profile).first()

        subtree = with_wiki_descendants([wiki])
        stash_for_undo("wiki", subtree, profile)
        for descendant in subtree:
            descendant.delete()

        redirect_url = reverse("pin.details", kwargs={"pin_slug": user_pin.slug}) if user_pin else reverse("map.view")
        response = HttpResponse("", status=200)
        response["HX-Redirect"] = redirect_url
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": "Community wiki deleted. Undo within 7 days from Settings → Undo History."}})
        return response


class LocationWikiEditView(LoginRequiredMixin, View):
    """Suggest (and immediately apply) a community edit to a Wiki's fields.

    POST /location/<slug>/wiki/edit/
    Body (JSON or form): field=value pairs for any subset of _WIKI_EDITABLE_FIELDS.
    Records a WikiEdit and applies changes to the Wiki.
    """

    def post(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            body = json.loads(request.body) if request.body else {}
        except (json.JSONDecodeError, ValueError):
            body = request.POST.dict()

        from datetime import datetime

        valid_security = {v for v, _ in SecurityLevel.choices}
        # new_vals holds the actual Python values to set on the wiki.
        # changes holds JSON-safe strings for the WikiEdit audit record.
        new_vals: dict[str, object] = {}
        changes: dict[str, dict] = {}
        for field in _WIKI_EDITABLE_FIELDS:
            if field not in body:
                continue
            raw = body[field]
            old_val = getattr(wiki, field, None)
            if str(raw) == str(old_val):
                continue
            if field in _WIKI_SECURITY_FIELDS:
                if raw not in valid_security:
                    continue
                new_val: object = raw
            elif field in {"date_abandoned", "date_last_active"}:
                if not raw:
                    new_val = None
                else:
                    try:
                        new_val = datetime.strptime(raw, "%Y-%m-%d").date()
                    except ValueError:
                        continue
            elif field == "description":
                length_error = text_length_error(raw, MAX_WIKI_DESCRIPTION_LENGTH, "Description")
                if length_error:
                    return JsonResponse({"error": length_error}, status=400)
                new_val = raw
            else:
                new_val = raw
            new_vals[field] = new_val
            changes[field] = {"from": str(old_val), "to": str(new_val)}

        if not changes:
            return JsonResponse({"ok": True, "message": "No changes detected."})

        # Apply wiki-field changes.
        for field, val in new_vals.items():
            setattr(wiki, field, val)
        wiki.save()

        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes=changes,
        )

        # Description, dates, and security indicators all render together in the
        # "About" card - send back the freshly-rendered fragment so the client can
        # swap it in place instead of leaving edited-but-unrendered fields stale.
        about_html = render_to_string("dashboard/partials/wiki/_wiki_about_card.html", {"wiki": wiki}, request=request)
        return JsonResponse({"ok": True, "changes": list(changes.keys()), "about_html": about_html})


def _render_history(request, location: Location, wiki: Wiki):
    """Render the edit-history partial, including the requester's own profile.

    Shared by the history list view and the revert/delete actions below so a
    successful action re-renders the up-to-date list in place, instead of
    leaving a stale row (or a raw JSON body) swapped into the DOM.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    edits = wiki.edits.select_related("editor__user", "reverted_by").order_by("-created")
    return render(
        request,
        "dashboard/pages/location/wiki_history.html",
        {"location": location, "wiki": wiki, "edits": edits, "current_profile": profile},
    )


def _revert_edit_fields(location: Location, wiki: Wiki, target_edit: WikiEdit) -> dict[str, dict]:
    """Restore the fields captured in ``target_edit.changes`` to their prior ("from") values.

    Mutates ``wiki`` (and any associated Boundary rows) in place - the caller
    is responsible for calling ``wiki.save()`` afterwards.

    Returns:
        A diff dict in the same ``{"field": {"from": ..., "to": ...}}`` shape
        used by ``WikiEdit.changes``, computed against the values as they
        stood right before this call.
    """
    revert_changes: dict[str, dict] = {}
    for field, diff in target_edit.changes.items():
        old_val = diff.get("from")
        if field == "bounding_box" or field.startswith("boundary_"):
            # "bounding_box" is the legacy audit key from the single-boundary
            # era; treat it as the property boundary.
            boundary_type = field.removeprefix("boundary_") if field.startswith("boundary_") else BoundaryType.PROPERTY
            if boundary_type not in BoundaryType.values:
                continue
            row = Boundary.objects.row_for_wiki(wiki, boundary_type)
            current_val = row.polygon.wkt if row and row.polygon else None
            revert_changes[field] = {"from": current_val, "to": old_val}
            if old_val:
                restored = GEOSGeometry(old_val, srid=4326)
                if isinstance(restored, Polygon):
                    restored = MultiPolygon(restored, srid=restored.srid)
                if row is None:
                    row = Boundary(wiki=wiki, location=location, boundary_type=boundary_type)
                row.polygon = restored
                row.save()
            elif row is not None:
                row.delete()
        elif field in {"latitude", "longitude"}:
            # Coordinates are no longer editable - a Wiki's Location is fixed
            # at creation. Skip so legacy WikiEdit rows recorded before this
            # rule don't error out on revert.
            continue
        else:
            current_val = getattr(wiki, field, None)
            revert_changes[field] = {"from": current_val, "to": old_val}
            setattr(wiki, field, old_val)
    return revert_changes


class LocationWikiHistoryView(LoginRequiredMixin, View):
    """HTMX partial: edit history list for a wiki.

    GET /location/<slug>/wiki/history/
    """

    def get(self, request, location_slug):
        location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        return _render_history(request, location, wiki)


class LocationWikiRevertView(LoginRequiredMixin, View):
    """Revert a specific WikiEdit.

    POST /location/<slug>/wiki/history/<edit_id>/revert/
    Creates a new WikiEdit that restores the "from" values and marks the
    original edit as reverted.
    """

    def post(self, request, location_slug, edit_id: int):
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        target_edit = get_object_or_404(WikiEdit, id=edit_id, wiki=wiki)

        if target_edit.reverted:
            return JsonResponse({"error": "This edit has already been reverted."}, status=400)

        revert_changes = _revert_edit_fields(location, wiki, target_edit)
        wiki.save()

        revert_edit = WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes=revert_changes,
        )
        target_edit.reverted = True
        target_edit.reverted_by = revert_edit
        target_edit.save(update_fields=["reverted", "reverted_by", "updated"])

        return _render_history(request, location, wiki)


class LocationWikiEditDeleteView(LoginRequiredMixin, View):
    """Permanently erase one of the requesting user's own wiki edits.

    POST /location/<slug>/wiki/history/<edit_id>/delete/

    Unlike a plain revert (which keeps the edit visible in history, just
    flagged as reverted), this restores the fields to their pre-edit values
    (if not already reverted) and hard-deletes the WikiEdit row - and its
    revert record, if any, since that also carries the original value as its
    "from" - so no copy of the data lingers anywhere. Intended for cases like
    accidentally pasting private information into a public wiki field.

    Only the editor who made the original edit may delete it.
    """

    def post(self, request, location_slug, edit_id: int):
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        target_edit = get_object_or_404(WikiEdit, id=edit_id, wiki=wiki)

        if target_edit.editor_id != profile.id:
            return JsonResponse({"error": "You can only permanently delete your own edits."}, status=403)

        if not target_edit.reverted:
            _revert_edit_fields(location, wiki, target_edit)
            wiki.save()

        revert_record = target_edit.reverted_by
        if revert_record is not None:
            revert_record.delete()
        target_edit.delete()

        return _render_history(request, location, wiki)


class WikiStatVoteView(LoginRequiredMixin, View):
    """Cast or clear the requester's vote on one community stat field.

    POST /location/<slug>/wiki/stat/<field>/vote/
    Body: ``value`` (1-5) to cast a vote, or 0/absent to clear it.

    Re-renders just that stat item - both the recalculated composite and the
    viewer's own vote - never a full page reload.
    """

    def post(self, request, location_slug, field):
        if field not in WikiStatField.values:
            raise Http404
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)

        try:
            value = int(request.POST.get("value") or 0)
        except (TypeError, ValueError):
            value = 0

        if 1 <= value <= 5:
            WikiStatVote.objects.update_or_create(wiki=wiki, profile=profile, field=field, defaults={"value": value})
        else:
            WikiStatVote.objects.filter(wiki=wiki, profile=profile, field=field).delete()

        return render(
            request,
            "dashboard/partials/pins/_wiki_stat_rating_item.html",
            {"wiki": wiki, **_wiki_stat_context(wiki, field, profile)},
        )
