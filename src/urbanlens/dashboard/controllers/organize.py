"""Organize controller - unified Tags + Categories management page."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User as AuthUser
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.labels.model import COLOR_CHOICES, ICON_CATEGORIES, ICON_CHOICES, KIND_MEDIA, KIND_USER, Label
from urbanlens.dashboard.models.pin.signals import refresh_map_pin_cache_for_label_ids

# Kinds that never affect map icon priority, and so are excluded from the
# Display Order tab (tag/category/status only).
_NON_PRIORITY_KINDS = (KIND_USER, KIND_MEDIA)

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_PERM = "dashboard.edit_global_label"
_LABEL_TABS = frozenset({"tags", "categories", "status", "people", "media", "priority"})
_SECTION_TABS = frozenset({"lists", "filters"})
_VALID_ORGANIZE_TABS = _LABEL_TABS | _SECTION_TABS

_BASE_CTX = {
    "icon_choices": ICON_CHOICES,
    "icon_categories": ICON_CATEGORIES,
    "color_choices": COLOR_CHOICES,
}


def build_organize_page_context(request: HttpRequest, active_tab: str = "tags") -> dict:
    """Build template context shared by the Organize page and per-kind standalone pages.

    Args:
        request: The HTTP request (used for profile and permissions).
        active_tab: Tab to show as active - one of the label tabs (tags, categories,
            status, people, priority) or one of the top-level sections (lists, filters).

    Returns:
        Context dict for dashboard/pages/organize/index.html. Includes both
        ``active_section`` (labels/lists/filters - which top-level subnav tab is
        current) and ``active_tab`` (which label sub-tab is current, only
        meaningful while ``active_section == "labels"``).
    """
    if not isinstance(request.user, AuthUser):
        raise TypeError("Expected an authenticated user")
    profile: Profile = request.user.profile
    active_section = active_tab if active_tab in _SECTION_TABS else "labels"
    label_tab = active_tab if active_tab in _LABEL_TABS else "tags"
    on_labels_section = active_section == "labels"

    def _rows_if_active(tab_key: str, queryset: QuerySet[Label]) -> list[Label]:
        """Materialize a label tab's card list only when it's the one on screen.

        Every other tab is toggled purely client-side (``installOrgTabSwitching``
        just flips ``hidden``), so rendering all of them server-side on every
        load means paying their full card-template cost - `{% include %}` per
        label, run for every kind - even for tabs nobody has looked at yet.
        `.with_hierarchy()` already dropped the pin-count *query* cost (see
        ``LabelQuerySet.with_hierarchy``); this drops the remaining Python/
        template cost, which query-count fixes never touch and which scales the
        same way: profiled at 500 tags, the initial page paint alone (six tabs'
        worth of cards, all but one invisible) cost ~12s of wall time against
        ~0.2s of actual database time. A tab rendered as empty here still gets
        real content the moment it's shown, via the same HTMX `hx-trigger=
        "revealed"` fetch that already backfills pin-count stats on the active
        tab (see ``organize_label_panel.html``) - only the *first* paint is
        skipped, not the tab.
        """
        if not on_labels_section or label_tab != tab_key:
            return []
        return list(queryset)

    # `.with_hierarchy()`, not `.with_pin_counts()` - the pin/location/total-pins
    # stats are the slow part of this page (a correlated subquery per label plus,
    # for any label with children, a full descendant BFS in `tag_total_pins`).
    # Render the cards without them so the page paints immediately; each tab's
    # rows re-fetch themselves with real counts via HTMX once shown (see
    # organize_label_panel.html's `hx-trigger="revealed"`).
    tags = _rows_if_active("tags", Label.objects.tags().visible_to(profile).in_display_order().with_customizations_for(profile).with_hierarchy())
    categories = _rows_if_active("categories", Label.objects.categories().for_profile(profile).in_display_order().with_customizations_for(profile).with_hierarchy())
    statuses = _rows_if_active("status", Label.objects.statuses().for_profile(profile).in_display_order().with_customizations_for(profile).with_hierarchy())
    user_labels = _rows_if_active("people", Label.objects.user_labels().visible_to(profile).in_display_order().with_customizations_for(profile).with_hierarchy())
    media_labels = _rows_if_active("media", Label.objects.media().visible_to(profile).in_display_order().with_hierarchy())
    # Always materialized, unlike the lists above: every tab's "create" dialog
    # needs it as the parent-picker's candidate list, not only the Display
    # Order tab that renders it directly. Already skips both the pin-count
    # annotation and the hierarchy prefetch, so evaluating it unconditionally
    # here was never the expensive part.
    priority_items = Label.objects.visible_to(profile).exclude(kind__in=_NON_PRIORITY_KINDS).in_display_order()
    # People/media have no `priority_items` equivalent (that queryset excludes
    # both kinds), and their own create dialog's parent-picker can't be fed
    # from `user_labels`/`media_labels` above once those are deferred - a
    # dialog rendered while its tab is inactive would otherwise get an empty
    # candidate list and never see the real one, since the HTMX reveal fetch
    # only replaces the rows, not the dialog. Plain and unprefetched, like
    # `priority_items`, so evaluating them unconditionally costs nothing.
    people_parent_items = Label.objects.user_labels().visible_to(profile).in_display_order()
    media_parent_items = Label.objects.media().visible_to(profile).in_display_order()

    return {
        **_BASE_CTX,
        "tags": tags,
        "categories": categories,
        "statuses": statuses,
        "user_labels": user_labels,
        "media_labels": media_labels,
        "priority_items": priority_items,
        "people_parent_items": people_parent_items,
        "media_parent_items": media_parent_items,
        "tags_deferred": not (on_labels_section and label_tab == "tags"),
        "categories_deferred": not (on_labels_section and label_tab == "categories"),
        "statuses_deferred": not (on_labels_section and label_tab == "status"),
        "people_deferred": not (on_labels_section and label_tab == "people"),
        "media_deferred": not (on_labels_section and label_tab == "media"),
        "priority_deferred": not (on_labels_section and label_tab == "priority"),
        "active_tab": label_tab,
        "active_section": active_section,
        "can_edit_global": request.user.has_perm(_PERM),
        "standalone_mode": False,
        "stats_pending": True,
    }


class OrganizeIndexView(LoginRequiredMixin, View):
    """Unified Organize page with Labels (Tags/Categories/Statuses/People/Priority), Lists, and Filters tabs."""

    def get(self, request, *args, **kwargs):
        """Render the organize page.

        Args:
            request: The HTTP request. Accepts ?tab=tags|categories|status|people|priority|lists|filters.

        Returns:
            Rendered organize/index.html.
        """
        tab = request.GET.get("tab", "tags")
        if tab not in _VALID_ORGANIZE_TABS:
            tab = "tags"
        return render(request, "dashboard/pages/organize/index.html", build_organize_page_context(request, tab))


class OrganizePriorityListView(LoginRequiredMixin, View):
    """Re-render the Display Order tab's priority list.

    GET /organize/priority/list/

    The initial page load renders this list once; any label create/edit/delete/
    merge/bulk-edit/convert elsewhere on the Organize page fires a `refreshPriority`
    client-side event (see organize.ts) that re-fetches it here, so a renamed,
    re-icon'd, deleted, or newly-created label shows up without a full reload.
    """

    def get(self, request, *args, **kwargs):
        """Render the priority-list partial.

        Args:
            request: The HTTP request.

        Returns:
            Rendered `_priority_list.html` partial.
        """
        if not isinstance(request.user, AuthUser):
            raise TypeError("Expected an authenticated user")
        profile: Profile = request.user.profile
        # _priority_list.html never renders pin counts - no need for with_pin_counts() here.
        priority_items = Label.objects.visible_to(profile).exclude(kind__in=_NON_PRIORITY_KINDS).in_display_order()
        return render(request, "dashboard/partials/labels/_priority_list.html", {"priority_items": priority_items})


class OrganizePrioritySaveView(LoginRequiredMixin, View):
    """Save the combined priority order for tags and categories."""

    def post(self, request, *args, **kwargs):
        """Persist new order for the submitted item IDs.

        Expects JSON body: {"items": [{"id": 1}, {"id": 2}, ...]} in display order
        (first item gets the highest order value).

        Only labels the requester *owns* are reordered. ``Label.order`` is a
        single shared column, and ``visible_to`` deliberately spans both a
        profile's own labels and the site-wide global ones - so validating
        against it and then writing without re-scoping meant every user's drag
        rewrote the ordering of shared labels for **everyone on the site**. Any
        global in the submission is skipped and named in ``skipped_global_ids``
        so the client can render those rows as position-locked rather than
        silently losing the user's gesture. Per-profile ordering of globals
        would need an ``order`` column on ``LabelCustomization``; until that
        exists, refusing is the only correct answer.

        Args:
            request: The HTTP request with JSON body.

        Returns:
            JSON response with ok=True, the number of labels reordered, and the
            ids skipped because they are global (and therefore not the
            requester's to reorder).
        """
        try:
            data = json.loads(request.body)
            item_ids = [int(x["id"]) for x in data.get("items", [])]
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            return JsonResponse({"error": "Invalid data"}, status=400)

        if not item_ids:
            return JsonResponse({"error": "No items provided"}, status=400)

        profile = request.user.profile
        # for_profile, not visible_to: the latter includes globals by design.
        owned_ids = set(Label.objects.for_profile(profile).filter(id__in=item_ids).values_list("id", flat=True))

        total = len(item_ids)
        reordered: list[Label] = []
        skipped_global_ids: list[int] = []
        for index, item_id in enumerate(item_ids):
            if item_id not in owned_ids:
                # Either global, or someone else's, or nonexistent - the three
                # are deliberately not distinguished in the response.
                skipped_global_ids.append(item_id)
                continue
            reordered.append(Label(id=item_id, order=total - index))

        if reordered:
            # One statement instead of N, inside a transaction: a partial
            # reorder is worse than none, since the user sees an arrangement
            # that was never what they dragged.
            with transaction.atomic():
                Label.objects.bulk_update(reordered, ["order"])
            # bulk_update fires no post_save, so the cache-invalidating receiver
            # never runs - and order decides which label supplies a pin's icon.
            refresh_map_pin_cache_for_label_ids([label.pk for label in reordered])

        return JsonResponse({"ok": True, "reordered": len(reordered), "skipped_global_ids": skipped_global_ids})
