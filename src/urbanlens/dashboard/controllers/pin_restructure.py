"""One suggestion, offered once per pin: organize this property's hierarchy.

Pinning a campus is one click; modelling it properly is a hundred more. And a
user who pinned those buildings individually - before child pins existed, or
via a bulk import - has them all sitting at the top level of their map when
they belong under one property. Both are offered together the first time the
owner opens the pin's detail page, as a single dialog with a single yes.

Endpoints, all pin-scoped:

- ``offer``   - the dialog, or a quiet 204 when there's nothing to suggest.
  Self-polls while the parcel's building lookup is still in flight, so nothing
  ever waits on REData.
- ``apply``   - do it: create the missing building pins and nest the matching
  top-level pins.
- ``dismiss`` - "No" (this pin, permanently) or "Don't show again" (turns the
  owner's ``suggest_pin_restructure`` setting off for every pin at once).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.parcel_buildings import building_rows
from urbanlens.dashboard.services.locations import site_scope
from urbanlens.dashboard.services.pins import pin_restructure
from urbanlens.dashboard.services.pins.pin_merge import PinMergeCollisionError, UnresolvedMergeConflictError, merge_pins, plan_merge_conflicts

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def _poll_attempt(request: HttpRequest) -> int:
    """Which poll cycle this request is (0 for the initial load).

    Mirrors ``PinController._poll_attempt`` - the suggestion shares the panel
    machinery's poll budget without being a panel itself.

    Args:
        request: The current request.

    Returns:
        The attempt number, never negative.
    """
    try:
        return max(int(request.GET.get("attempt", "0")), 0)
    except (TypeError, ValueError):
        return 0


def _toast(response: HttpResponse, level: str, message: str, *, refresh: bool = False) -> HttpResponse:
    """Attach a toast (and optionally a refresh event) to an HTMX response."""
    triggers: dict = {"showToast": {"level": level, "message": message}}
    if refresh:
        triggers["pinDetailPinsChanged"] = True
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _dialog_rows(buildings: list[dict]) -> list[dict]:
    """Building-panel rows decorated with server-validated selection keys."""
    keyed = [{**building, "_selection_key": pin_restructure.building_selection_key(building)} for building in buildings]
    return building_rows(keyed, [])


def _selected_buildings(request: HttpRequest, buildings: list[dict]) -> list[dict]:
    """Honor an explicit dialog selection; retain all-building legacy POSTs."""
    if "building_selection" not in request.POST:
        return buildings
    return pin_restructure.select_buildings(buildings, request.POST.getlist("building_keys"))


def _nestable_rows(pin: Pin, nestable: list[Pin], *, default_merge_pks: frozenset[int] = frozenset()) -> list[dict]:
    """Per-candidate context for the dialog's "Pins on this property" list.

    Conflicts are computed fresh for every candidate up front (not only after
    a failed merge attempt) so the dialog can warn about them before the owner
    ever picks "Merge" - mirroring how the single-pair ``PinMergeSuggestion``
    queue always shows its conflict pickers rather than waiting for a rejected
    submission (see ``pin_merge_suggestions.merge_suggestion_cards``).

    Args:
        pin: The property pin - always the merge survivor in this flow.
        nestable: Candidates still worth offering (``plan.nestable``, or just
            the ones left unresolved after a partial apply).
        default_merge_pks: Candidate pks whose mode should default to "Merge"
            rather than "Child pin" - set on the resubmit-with-conflicts
            render so a candidate the owner already chose to merge doesn't
            silently revert to nesting.

    Returns:
        One ``{"pin", "conflicts", "default_merge"}`` dict per candidate.
    """
    return [
        {
            "pin": candidate,
            "conflicts": plan_merge_conflicts(pin, candidate),
            "default_merge": candidate.pk in default_merge_pks,
        }
        for candidate in nestable
    ]


def _nestable_map_data(nestable: list[Pin]) -> list[dict]:
    """Plain, JSON-safe rows for the dialog's preview map - see map-annotations.ts."""
    return [
        {
            "pk": candidate.pk,
            "name": candidate.effective_name,
            "latitude": candidate.effective_latitude,
            "longitude": candidate.effective_longitude,
        }
        for candidate in nestable
        if candidate.effective_latitude is not None and candidate.effective_longitude is not None
    ]


def _dialog_context(
    request: HttpRequest,
    pin: Pin,
    buildings: list[dict],
    *,
    restructure: bool,
    nestable: Sequence[Pin] = (),
    default_merge_pks: frozenset[int] = frozenset(),
) -> dict:
    """Context shared by the full dialog render and its body-only resubmit render."""
    nestable = list(nestable)
    return {
        "pin": pin,
        "rows": _dialog_rows(buildings),
        "building_count": len(buildings),
        "restructure": restructure,
        "nestable_rows": _nestable_rows(pin, nestable, default_merge_pks=default_merge_pks) if restructure else [],
        "nestable_map_data": _nestable_map_data(nestable) if restructure else [],
        "form_action": request.path,
    }


def _dialog_title(ctx: dict) -> str:
    """Pick a title that describes what's actually in the dialog."""
    has_buildings = bool(ctx["rows"])
    has_pins = bool(ctx["nestable_rows"])
    if has_buildings and has_pins:
        return "Choose buildings and pins to organize"
    if has_pins:
        return "Choose pins to organize"
    return "Choose buildings to add"


def _render_building_dialog(
    request: HttpRequest,
    pin: Pin,
    buildings: list[dict],
    *,
    restructure: bool,
    nestable: Sequence[Pin] = (),
    default_merge_pks: frozenset[int] = frozenset(),
) -> HttpResponse:
    """Render the full dialog (header + body) - used to open it fresh."""
    ctx = _dialog_context(request, pin, buildings, restructure=restructure, nestable=nestable, default_merge_pks=default_merge_pks)
    ctx["dialog_title"] = _dialog_title(ctx)
    return render(request, "dashboard/partials/pins/_building_import_dialog.html", ctx)


def _render_building_dialog_body(
    request: HttpRequest,
    pin: Pin,
    buildings: list[dict],
    *,
    restructure: bool,
    nestable: Sequence[Pin] = (),
    default_merge_pks: frozenset[int] = frozenset(),
) -> HttpResponse:
    """Render just the swappable body - a POST response that keeps the dialog open."""
    ctx = _dialog_context(request, pin, buildings, restructure=restructure, nestable=nestable, default_merge_pks=default_merge_pks)
    response = render(request, "dashboard/partials/pins/_building_import_dialog_body.html", ctx)
    response["HX-Keep-Open"] = "1"
    return response


class PinRestructureOfferView(LoginRequiredMixin, View):
    """GET: the restructure suggestion for this pin, or a quiet 204."""

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, schedule_panel_fetch

        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        if not pin_restructure.should_offer(pin):
            return HttpResponse(status=204)

        if site_scope.parcel_buildings(pin.location) is None:
            # The parcel's buildings have never been looked up. Schedule it and
            # poll, rather than making the page wait on a REData round-trip -
            # the nesting half of the suggestion is worth waiting for it too,
            # since a plan showing only half of what it will do reads as a bug.
            attempt = _poll_attempt(request)
            if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(site_scope.PARCEL_BUILDINGS_CACHE_SOURCE, pin):
                return self._render_plan(request, pin)
            return render(
                request,
                "dashboard/partials/pins/_pin_restructure_pending.html",
                {"poll_url": request.path, "next_attempt": attempt + 1, "poll_interval": POLL_INTERVAL_SECONDS},
            )

        return self._render_plan(request, pin)

    @staticmethod
    def _render_plan(request: HttpRequest, pin: Pin) -> HttpResponse:
        """Render the dialog for whatever this pin's plan turns out to be."""
        plan = pin_restructure.plan_for(pin)
        if plan.is_empty:
            return HttpResponse(status=204)
        return render(
            request,
            "dashboard/partials/pins/_pin_restructure_offer.html",
            {
                "pin": pin,
                "buildings": plan.buildings,
                "building_count": len(plan.buildings),
                "nestable": plan.nestable[:5],
                "nestable_count": len(plan.nestable),
                "sample_names": [name for building in plan.buildings[:3] if (name := pin_restructure.building_name(building))],
            },
        )


class PinRestructureDismissView(LoginRequiredMixin, View):
    """POST: decline the suggestion, for this pin or for every pin.

    ``?scope=all`` is the dialog's "Don't show again" - it turns the owner's
    ``suggest_pin_restructure`` setting off, so the suggestion stops appearing
    everywhere, and still marks this pin so the setting can be turned back on
    later without this particular pin re-asking.
    """

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("profile"), slug=pin_slug, profile__user=request.user)
        pin.restructure_offer_dismissed = True
        pin.save(update_fields=["restructure_offer_dismissed", "updated"])

        response = HttpResponse("", status=200)
        if request.GET.get("scope") == "all":
            profile = pin.profile
            profile.suggest_pin_restructure = False
            profile.save(update_fields=["suggest_pin_restructure"])
            return _toast(response, "info", "Pin organization suggestions turned off. You can turn them back on in Settings → Map.")
        return response


class PinRestructureApplyView(LoginRequiredMixin, View):
    """GET the chooser; POST selected buildings and matching top-level pins.

    Idempotent in the way that matters: the plan is recomputed here rather than
    trusted from the page, so buildings pinned (or pins nested) since the dialog
    rendered are simply skipped.
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        plan = pin_restructure.plan_for(pin)
        if plan.is_empty:
            return HttpResponse(status=204)
        return _render_building_dialog(request, pin, plan.buildings, restructure=True, nestable=plan.nestable)

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        plan = pin_restructure.plan_for(pin)
        if plan.is_empty:
            return _toast(HttpResponse("", status=200), "info", "This property is already organized.")

        buildings = _selected_buildings(request, plan.buildings)
        created = pin_restructure.create_building_pins(pin, buildings)
        _queue_wiki_mirror(pin, buildings)

        # Mirrors _selected_buildings, but keyed on its own marker rather than
        # building_selection: a legacy/no-body POST (this view used to accept
        # a bare POST and nest unconditionally) must keep nesting every
        # candidate, while a real all-unchecked *pins* selection from the
        # current dialog is a deliberate choice - "every one of these is a
        # duplicate, merge them all" - not the absence of one.
        if "nest_selection" in request.POST:
            included = set(request.POST.getlist("nest_keys"))
            candidates = [candidate for candidate in plan.nestable if str(candidate.pk) in included]
        else:
            candidates = list(plan.nestable)

        to_nest: list[Pin] = []
        to_merge: list[Pin] = []
        for candidate in candidates:
            if request.POST.get(f"nest_mode__{candidate.pk}") == "merge":
                to_merge.append(candidate)
            else:
                to_nest.append(candidate)

        nested = pin_restructure.nest_root_pins(pin, to_nest)

        merged = 0
        unresolved: list[Pin] = []
        collision_messages: list[str] = []
        for candidate in to_merge:
            conflicts = plan_merge_conflicts(pin, candidate)
            resolutions: dict[str, int] = {}
            for conflict in conflicts:
                raw = request.POST.get(f"resolution__{candidate.pk}__{conflict.key}", "")
                if raw.isdigit():
                    resolutions[conflict.key] = int(raw)
            try:
                merge_pins(survivor=pin, loser=candidate, profile=pin.profile, resolutions=resolutions)
                merged += 1
            except UnresolvedMergeConflictError:
                unresolved.append(candidate)
            except PinMergeCollisionError as exc:
                collision_messages.append(exc.safe_message)

        if unresolved:
            # Recomputing the plan would drop these candidates too (merge_pins
            # never touched them, but plan_for's own query re-runs regardless) -
            # explicit default_merge_pks keeps "Merge" selected for exactly the
            # ones still needing a choice, instead of reverting to "Child pin".
            # refresh=True: buildings/nests/other merges above may already have
            # changed this pin's children, so the page's own panels (and the
            # organize suggestion card, via its pinDetailPinsChanged listener)
            # should catch up even though the dialog itself stays open.
            response = _render_building_dialog_body(
                request,
                pin,
                [],
                restructure=True,
                nestable=unresolved,
                default_merge_pks=frozenset(candidate.pk for candidate in unresolved),
            )
            return _toast(response, "error", "Resolve the highlighted differences to finish merging.", refresh=True)

        parts = []
        if created:
            parts.append(f"Added {created} building pin{'s' if created != 1 else ''}.")
        if nested:
            parts.append(f"Nested {nested} existing pin{'s' if nested != 1 else ''} under this property.")
        if merged:
            parts.append(f"Merged {merged} pin{'s' if merged != 1 else ''} into this one.")
        parts.extend(collision_messages)
        level = "warning" if collision_messages else "success"
        return _toast(HttpResponse("", status=200), level, " ".join(parts) or "Nothing left to organize.", refresh=True)


def _queue_wiki_mirror(pin, buildings) -> None:
    """Mirror an import onto the community wiki in the background.

    Deliberately fire-and-forget: the child pins already exist by now, so a
    wiki-side failure must not turn a successful pin action into a 500 (see
    docs/PROBLEMS.md, 2026-08-18).
    """
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import mirror_buildings_to_wiki

    keys = [pin_restructure.building_selection_key(building) for building in buildings]
    safely_enqueue_task(mirror_buildings_to_wiki, pin.pk, keys)


class PinBuildingImportView(LoginRequiredMixin, View):
    """GET the chooser; POST selected unpinned buildings on this property.

    The "Buildings on this Property" panel's own action, distinct from the
    restructure suggestion above: always available (never dismissed), and
    scoped strictly to buildings - it never re-parents anything.
    """

    def get(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)
        missing = pin_restructure.missing_buildings(pin)
        if not missing:
            return HttpResponse(status=204)
        return _render_building_dialog(request, pin, missing, restructure=False)

    def post(self, request: HttpRequest, pin_slug: str) -> HttpResponse:
        pin = get_object_or_404(Pin.objects.select_related("location", "profile"), slug=pin_slug, profile__user=request.user)

        missing = pin_restructure.missing_buildings(pin)
        if not missing:
            return _toast(HttpResponse("", status=200), "info", "Every building here already has a pin.")

        buildings = _selected_buildings(request, missing)
        if not buildings:
            return _toast(HttpResponse("", status=200), "info", "Select at least one building to add.")

        created = pin_restructure.create_building_pins(pin, buildings)
        _queue_wiki_mirror(pin, buildings)

        if not created:
            # Every selected building was skipped - each one's point already
            # carries a pin of this profile's. Saying "Added 0 building pins"
            # over a success toast is how this read as a silent failure; still
            # refresh, so the panel and the suggestion re-derive their counts.
            return _toast(HttpResponse("", status=200), "info", "Those buildings already have your pins on them - nothing new to add.", refresh=True)

        message = f"Added {created} building pin{'s' if created != 1 else ''}."
        return _toast(HttpResponse("", status=200), "success", message, refresh=True)
