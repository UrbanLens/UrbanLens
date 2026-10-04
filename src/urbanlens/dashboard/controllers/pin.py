from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import logging
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError
from rest_framework.viewsets import GenericViewSet

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.controllers.map_overlays import OVERLAY_UUID_PLACEHOLDER, overlay_payload
from urbanlens.dashboard.controllers.temporal_imagery import TEMPORAL_YEAR_PLACEHOLDER
from urbanlens.dashboard.forms.upload_datafile import UploadDataFile
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.labels.meta import KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_if_small
from urbanlens.dashboard.services.core.pagination import get_page
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
from urbanlens.dashboard.services.core.request_body import drf_data_object
from urbanlens.dashboard.services.locations.site_scope import rederive_pin_type
from urbanlens.dashboard.services.locations.temporal_imagery import temporal_slider_years
from urbanlens.dashboard.services.search.search import search_web
from urbanlens.dashboard.services.security.redact import redact_coordinate
from urbanlens.dashboard.services.security.throttle import Rate
from urbanlens.dashboard.services.wiki.wiki_seed import seed_pin_from_cached_wikipedia
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence
    from uuid import UUID

    from rest_framework.request import Request

    from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, StreetViewSlide
    from urbanlens.dashboard.services.pins.external_data import InfoPanelSource, LocationCachePanelSource, PanelSource, ProviderFetchResult
    from urbanlens.dashboard.services.pins.panel_probe import PanelProbe

logger = logging.getLogger(__name__)

_SlideT = TypeVar("_SlideT")

_WEB_SEARCH_CLIENT_PAGE_SIZE = 5
# Preview limit for own photos in Media "All" view.
_MEDIA_PHOTOS_PREVIEW_LIMIT = 12
_ADAPTIVE_PAGE_BATCH_MULTIPLIER = 2
_WEB_SEARCH_PAGE_SIZE = _WEB_SEARCH_CLIENT_PAGE_SIZE * _ADAPTIVE_PAGE_BATCH_MULTIPLIER
_WEB_SEARCH_MIN_REFRESH_AGE = timedelta(days=1)

# Drag-to-resize bounds for the pin map.
_MAP_HEIGHT_MIN_PX = 320
_MAP_HEIGHT_MAX_PX = 1200

#: Location Data's bespoke tab, summarized in its Overview ahead of the placed tabs.
_LOCATION_DATA_BESPOKE_KEYS = ("nominatim",)


def _favicon_url(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?{urlencode({'domain': domain, 'sz': 16})}"


def _with_local_images(results: Iterable[dict]) -> list[dict]:
    """Web-search results with their thumbnail and favicon pointing at this site's copies, never at another host.

    Args:
        results: One page of cached results.

    Returns:
        Copies of the results carrying ``thumbnail`` and ``favicon`` in-app URLs, ``""`` where there is none.
    """
    from urbanlens.dashboard.services.media.remote_copies import RemoteImage, copy_urls

    results = [dict(result) for result in results]
    wanted = [RemoteImage(result.get("thumbnail") or "", "web_search", result.get("link") or "") for result in results]
    wanted += [RemoteImage(_favicon_url(result["domain"]), "favicon") for result in results if result.get("domain")]
    copies = copy_urls(wanted)
    for result in results:
        result["thumbnail"] = copies.get(result.get("thumbnail") or "", "")
        result["favicon"] = copies.get(_favicon_url(result["domain"]), "") if result.get("domain") else ""
    return results


def _with_local_slides[SlideT: (SatelliteSlide, StreetViewSlide)](slides: Sequence[SlideT], service_key: str) -> list[SlideT]:
    """Carousel slides showing this site's copies of their pictures.

    A slide dated "Current" is an address whose picture the provider replaces, so it is copied once a month.

    Args:
        slides: The slides as the providers gave them.
        service_key: The carousel, kept with the source as each copy's provenance.

    Returns:
        The slides, each remote ``img_src`` replaced by its copy's address.
    """
    from urbanlens.dashboard.services.media.remote_copies import RemoteImage, copy_urls

    month = timezone.now().strftime("%Y-%m")
    copies = copy_urls(RemoteImage(slide.img_src, f"{service_key}:{slide.source}", edition=month if slide.date == "Current" else "") for slide in slides)
    return [replace(slide, img_src=copies.get(slide.img_src, slide.img_src)) for slide in slides]


def _viewer_may_see_panel(request: HttpRequest, source: PanelSource) -> bool:
    """Whether the user holds the feature a panel requires.

    Args:
        request: The current request, for its authenticated user.
        source: The panel source being considered.

    Returns:
        True when unrestricted or the viewer holds the required feature.
    """
    from urbanlens.dashboard.services.pins.external_data import panel_visible_to

    return panel_visible_to(request.user, source)


def _visible_panel_sources(request: HttpRequest) -> list[PanelSource]:
    """Every registered panel source the viewer may see, in registry order."""
    from urbanlens.dashboard.services.pins.external_data import panel_sources

    return [source for source in panel_sources().values() if _viewer_may_see_panel(request, source)]


def _property_tab_sources(sources: Iterable[PanelSource], pin: Pin) -> list[InfoPanelSource]:
    """The Property Records card's tabs after its Overview, in tab order.

    Args:
        sources: The panel sources the viewer may see.
        pin: The pin whose page holds the card.

    Returns:
        The card's tab panels; a parcel's building characteristics belong to its buildings, so a parcel has none.
    """
    from urbanlens.dashboard.services.locations.site_scope import is_site_scope
    from urbanlens.dashboard.services.pins.external_data import PanelPlacement, tabbed_panels

    site_scope = is_site_scope(pin)
    return [source for source in tabbed_panels(sources, PanelPlacement.PROPERTY) if not (site_scope and source.key == "overture_building_attributes")]


#: Cards of the Private Pin page with views of their own, each placed by its own section of the template.
_BESPOKE_CARD_KEYS = ("azure_maps", "parcel_buildings", "yelp", "nps", "loopnet", "usgs_topo", "wikipedia")


def _probe_page_panels(request: HttpRequest, pin: Pin, info_panels: Sequence[InfoPanelSource], visible: Sequence[PanelSource]) -> tuple[PanelProbe, list[str]]:
    """Which of the Private Pin page's enrichment panels to leave out, because they are known to have nothing (P53).

    Args:
        request: The current request, for its viewer.
        pin: The pin being viewed.
        info_panels: The info panels the page may request as it opens: its standalone cards and Regional Data tabs.
        visible: Every panel source the viewer may see.

    Returns:
        The probe, and the Media gallery providers to load, in loader order. A debug-overlay viewer loads every
        provider, since an empty one still reports what it searched for.
    """
    from urbanlens.dashboard.services.admin.debug_overlay import can_view_debug_overlay
    from urbanlens.dashboard.services.photos.pin_photos import PIN_MEDIA_GALLERY_SOURCES
    from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, LocationCachePanelSource, panels_shown_in
    from urbanlens.dashboard.services.pins.panel_probe import probe_panels

    by_key = {source.key: source for source in visible}
    cards: dict[str, list[LocationCachePanelSource]] = {source.key: [source, *panels_shown_in(source, visible)] for source in info_panels}
    cards.update({key: [source] for key in _BESPOKE_CARD_KEYS if isinstance(source := by_key.get(key), LocationCachePanelSource)})
    galleries = [source for key in PIN_MEDIA_GALLERY_SOURCES if isinstance(source := by_key.get(key), GalleryMediaSource)]
    probe = probe_panels(pin, cards=cards, galleries=[] if can_view_debug_overlay(request.user) else galleries)
    return probe, [source.key for source in galleries if source.key not in probe.empty_galleries]


class PinController(LoginRequiredMixin, GenericViewSet):
    """
    Controller for the pin page
    """

    def view(self, request: HttpRequest, **kwargs):
        """
        View the pin page
        """
        from datetime import date

        from django.db.models import Case, When

        from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias
        from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES
        from urbanlens.dashboard.services.comments.comments import visible_comment_count

        try:
            pin = Pin.objects.select_related("location", "parent_pin", "parent_pin__location").get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            try:
                pin = Pin.objects.select_related("location", "parent_pin", "parent_pin__location").get(uuid=kwargs["pin_slug"], profile__user=request.user)
            except (Pin.DoesNotExist, ValueError, ValidationError):
                # ValidationError: pin_slug isn't a valid UUID string at all.
                return render(
                    request,
                    "dashboard/pages/errors/pin_not_found.html",
                    {"pin_slug": kwargs.get("pin_slug")},
                    status=404,
                )

        pin.backfill_wiki_link_slugs()
        pin.mark_viewed()
        # What other accounts' lookups found reaches the pin only on its owner's own visit.
        rederive_pin_type(pin)
        seed_pin_from_cached_wikipedia(pin)

        profile, _ = Profile.objects.get_or_create(user=request.user)

        today = timezone.localdate()
        min_date = date(today.year - 100, today.month, today.day)

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

        from urbanlens.dashboard.services.admin.debug_overlay import can_view_debug_overlay
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope
        from urbanlens.dashboard.services.pins.child_buildings import building_children, child_details_default
        from urbanlens.dashboard.services.places.scope import scope_badge

        # Parcel pins show child content in place of building cards.
        site_scope = is_site_scope(pin)

        building_child_count = building_children(pin).count()
        include_children = request.GET.get("children", "1" if child_details_default(pin, building_child_count) else "0") == "1"

        from urbanlens.dashboard.models.pin_list.model import PinList

        pin_lists = list(PinList.objects.for_profile(profile).with_pin_counts().order_by("name"))

        from urbanlens.dashboard.services.pins.external_data import PanelPlacement, own_panels, panel_readiness, tabbed_panels

        # Filter gated sources once so all surfaces stay consistent.
        visible_sources = _visible_panel_sources(request)
        all_info_panels = own_panels(visible_sources)
        regional_sources = tabbed_panels(all_info_panels, PanelPlacement.REGIONAL)
        panel_tabs = [{"key": source.key, "label": source.label, "icon": source.icon} for source in regional_sources]
        location_data_tabs = [{"key": source.key, "label": source.label, "icon": source.icon} for source in tabbed_panels(all_info_panels, PanelPlacement.LOCATION)]
        property_tabs = [{"key": source.key, "label": source.label, "icon": source.icon} for source in _property_tab_sources(all_info_panels, pin)]
        simple_info_panels = [source for source in all_info_panels if source.placement == PanelPlacement.STANDALONE and not (site_scope and source.key == "redata_building_attributes")]
        panel_probe, media_loaders = _probe_page_panels(request, pin, [*simple_info_panels, *regional_sources], visible_sources)

        # Show the first tab with fresh cached data that has something to show.
        # Bulk readiness check to avoid per-tab queries.
        tab_readiness = panel_readiness(pin, regional_sources)
        default_panel_tab_key = next((tab["key"] for tab in panel_tabs if tab_readiness[tab["key"]] and tab["key"] not in panel_probe.empty_cards), None)

        # True once aliases used on any pin, to dismiss onboarding.
        has_ever_used_aliases = PinAlias.objects.filter(pin__profile=profile).exists()

        from django.urls import reverse

        from urbanlens.dashboard.services.places.ambiguity import linked_wiki_locations

        custom_layers = list(CustomLayer.objects.for_pin(pin).order_by("order", "created"))

        return render(
            request,
            "dashboard/pages/location/index.html",
            {
                "pin": pin,
                "custom_layers": custom_layers,
                "custom_layers_json": [layer.to_json() for layer in custom_layers],
                "manage_layers_url": reverse("pin.layers", args=[pin.slug]),
                "map_overlays_json": overlay_payload(MapImageOverlay.objects.for_pin(pin)),
                "manage_overlays_url": reverse("pin.overlays", args=[pin.slug]),
                "manage_overlays_historical_url": reverse("pin.overlays.historical", args=[pin.slug]),
                "overlay_corners_url_template": reverse("pin.overlays.corners", args=[pin.slug, OVERLAY_UUID_PLACEHOLDER]),
                "temporal_slider_years": temporal_slider_years(pin.location, request.user) if pin.location_id else [],
                "temporal_imagery_url_template": reverse("pin.temporal_imagery", args=[pin.slug, TEMPORAL_YEAR_PLACEHOLDER]),
                "profile": profile,
                "parent_pin": pin.parent_pin,
                "has_child_pins": pin.detail_pins.exists(),
                "is_site_scope": site_scope,
                **scope_badge(pin),
                # The hero's wiki box renders from this on first paint; the overview's out-of-band swap only refreshes it.
                "linked_wiki_locations": linked_wiki_locations(pin, profile),
                "include_children": include_children,
                "building_child_count": building_child_count,
                "can_view_debug_overlay": can_view_debug_overlay(request.user),
                "google_maps_api_key": settings.google_unrestricted_api_key,
                "openweathermap_api_key": settings.openweathermap_api_key,
                "page_name": "location-details",
                "pin_alias_suggestions": pin.aliases.order_by(Case(When(kind=AliasType.OFFICIAL, then=0), default=1), "name"),
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
                "default_map_view": profile.default_map_view,
                "markup_fill_color": profile.markup_fill_color,
                "markup_fill_opacity": profile.markup_fill_opacity,
                "markup_border_color": profile.markup_border_color,
                "markup_border_opacity": profile.markup_border_opacity,
                "today": today.isoformat(),
                "min_date": min_date.isoformat(),
                "security_level_choices": SecurityLevel.choices,
                "pin_lists": pin_lists,
                "pin_cover_candidates": pin.cover_candidates(),
                "simple_info_panels": simple_info_panels,
                "empty_panels": panel_probe.empty_cards,
                "media_loaders": media_loaders,
                "panel_tabs": panel_tabs,
                "default_panel_tab_key": default_panel_tab_key,
                "location_data_tabs": location_data_tabs,
                "property_tabs": property_tabs,
                "has_ever_used_aliases": has_ever_used_aliases,
                "pin_comment_count": visible_comment_count(pin.comments.all(), profile),
                "pin_visit_count": pin.visit_history.count(),
                "media_bulk_actions": [
                    {"action": "relevant", "icon": "thumb_up", "label": "Mark relevant"},
                    {"action": "not_relevant", "icon": "thumb_down", "label": "Mark not relevant"},
                    {"action": "wiki", "icon": "public", "label": "Send to wiki"},
                ],
                "detail_pin_bulk_actions": [
                    {"action": "edit", "icon": "edit", "label": "Edit"},
                    {"action": "promote", "icon": "move_up", "label": "Promote to top level"},
                    {"action": "wiki", "icon": "public", "label": "Send to wiki"},
                    {"action": "share", "icon": "ios_share", "label": "Share with a friend"},
                    {"action": "delete", "icon": "delete", "label": "Delete"},
                ],
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
                "show_map_footer": True,
            },
        )

    def _debug_entry(self, request: HttpRequest, source: str, query: str, *, from_cache: bool, count: int | None = None):
        """Build a `DebugEntry` for the external-API debug overlay, admins only.

        Args:
            request: The current HttpRequest.
            source: Short identifier for the data source (e.g. ``"wikipedia"``).
            query: The search term, address, or coordinates used for the lookup.
            from_cache: Whether the result was served from cache.
            count: Number of results the lookup produced, when meaningful.

        Returns:
            A `DebugEntry`, or None if the requesting user can't view debug info.
        """
        from urbanlens.dashboard.services.admin.debug_overlay import DebugEntry, can_view_debug_overlay

        if not can_view_debug_overlay(request.user):
            return None
        return DebugEntry(source=source, query=query, from_cache=from_cache, count=count)

    # -- Async external-data panel plumbing -------------------------------------- External-data panels never
    # fetch upstream data on the request path: on a store miss the controller schedules a Celery task
    # (single-flight) and returns a self-polling placeholder; polls re-enter the same endpoint with ?attempt=N
    # until the task lands the data or the attempt budget runs out.

    @staticmethod
    def _poll_attempt(request: HttpRequest) -> int:
        """Which poll cycle this request is (0 for the initial page-load request)."""
        try:
            return max(int(request.GET.get("attempt", "0")), 0)
        except (TypeError, ValueError):
            return 0

    def _pending_panel(self, request: HttpRequest, pin: Pin, source_key: str, hide_tab_id: str | None = None, section_id: str | None = None, *, in_tab: bool = False):
        """Schedule a panel fetch and return its polling placeholder.

        Args:
            request: The current request.
            pin: The pin whose panel data is being fetched.
            source_key: An ``external_data.panel_sources()`` key.
            hide_tab_id: DOM id to hide on 204, if any.
            section_id: The placeholder's DOM id, when not the source's own.
            in_tab: Whether the placeholder fills a tab body - see :meth:`_pending_placeholder`.

        Returns:
            The placeholder fragment, or 204 when suppressed or exhausted.
        """
        from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS, get_panel_source, schedule_panel_fetch

        attempt = self._poll_attempt(request)
        if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(source_key, pin):
            return HttpResponse(status=204)
        source = get_panel_source(source_key)
        if source is None:
            return HttpResponse(status=204)
        return self._pending_placeholder(request, source, section_id=section_id or source.section_id, in_tab=in_tab, hide_tab_id=hide_tab_id)

    def _pending_placeholder(self, request: HttpRequest, source: PanelSource, *, section_id: str, in_tab: bool, title: str | None = None, hide_tab_id: str | None = None) -> HttpResponse:
        """The self-polling placeholder for a panel whose fetch is in flight.

        Args:
            request: The current request; its path is polled.
            source: The panel being fetched.
            section_id: The placeholder's DOM id.
            in_tab: Whether it fills a tab body, where it is the tab's only content and so shows its spinner. A card of
                its own stays hidden until there is something to show.
            title: The heading, when not the source's own title.
            hide_tab_id: DOM id to hide on 204, if any.

        Returns:
            The placeholder fragment.
        """
        from urbanlens.dashboard.services.pins.external_data import POLL_INTERVAL_SECONDS

        return render(
            request,
            "dashboard/partials/pins/panel_pending.html",
            {
                "section_id": section_id,
                "outer_class": source.outer_class,
                "outer_is_card": source.outer_is_card,
                "icon": source.icon,
                "title": title or source.title,
                "poll_url": request.path,
                "next_attempt": self._poll_attempt(request) + 1,
                "poll_interval": POLL_INTERVAL_SECONDS,
                "hide_tab_id": hide_tab_id,
                "in_tab": in_tab,
            },
        )

    @staticmethod
    def _notify_panel_ready(request: HttpRequest, response: HttpResponse, *events: str) -> HttpResponse:
        """Notify sibling panels to refresh via HX-Trigger.

        Args:
            request: The current request.
            response: The response to annotate. *events: Client event names other panels listen for.

        Returns:
            The same response, for chaining.
        """
        if PinController._poll_attempt(request) < 1:
            return response
        response["HX-Trigger"] = json.dumps(dict.fromkeys(events, True))
        return response

    def _pending_media(self, request: HttpRequest, pin: Pin, source_key: str):
        """Schedule a media provider's fetch and return its polling loader.

        The pending response therefore (a) retargets the swap back onto the requesting loader itself via
        HX-Retarget/HX-Reswap, and (b) carries the UL-Panel-Pending header so the gallery JS ignores it
        instead of counting it as a provider result.

        Args:
            request: The current request (its path doubles as the poll URL).
            pin: The pin whose media is being fetched.
            source_key: One of the media ``panel_sources()`` keys.

        Returns:
            The self-polling loader fragment, or a 204 when the source is suppressed or the poll budget is
            exhausted (the gallery JS counts a 204 as...
        """
        from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, schedule_panel_fetch

        attempt = self._poll_attempt(request)
        if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(source_key, pin):
            return HttpResponse(status=204)
        response = render(
            request,
            "dashboard/partials/pins/media_loader_pending.html",
            {
                "source": source_key,
                "poll_url": request.path,
                "next_attempt": attempt + 1,
                "poll_interval": POLL_INTERVAL_SECONDS,
            },
        )
        response["UL-Panel-Pending"] = "1"
        response["HX-Retarget"] = f"#media-loader-{source_key}"
        response["HX-Reswap"] = "outerHTML"
        return response

    def media_provider(self, request: HttpRequest, pin_slug: str, source: str):
        """
        HTMX partial: captioned media items for the pin's location from a single provider.

        Backs the combined "Media" section on the Private Pin page. Each provider
        (Smithsonian, Wikimedia Commons, Library of Congress, Yelp, Google
        Images, Google Maps, ...) is fetched via its own HTMX request targeting
        the shared gallery grid (see ``media-gallery-section`` in the pin detail
        template), so a slow provider never blocks the others from appearing.
        Every provider is a ``GalleryMediaSource``, so this view is oblivious to
        which one it's rendering - except ``"photos"``, a synchronous read
        straight off the pin's own uploaded ``Image`` rows rather than an
        async ``LocationCache``-backed external fetch (there's no external API
        call to warm, so the whole async-panel machinery would be pure
        overhead) - see ``_photos_media_preview``.
        """
        if source == "photos":
            return self._photos_media_preview(request, pin_slug)

        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
        from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, get_panel_source, panel_visible_to

        panel = get_panel_source(source)
        if not isinstance(panel, GalleryMediaSource):
            return HttpResponse(status=404)

        # Same gate the generic info-panel dispatch applies (_viewer_may_see_panel) - a feature-gated source's
        # photos must not leak through this separate gallery route just because it has no required_feature check
        # of its own.
        if not panel_visible_to(request.user, panel):
            return HttpResponse(status=404)

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        if not panel.gate(pin):
            return HttpResponse(status=204)

        cached = panel.cached_entry(pin)
        # A row whose media half was never filled in is not an answer for this
        # gallery, even though it is one for the info panel sharing the row.
        if cached is None or not panel.media_is_ready(cached.data or {}):
            return self._pending_media(request, pin, source)

        from urbanlens.dashboard.services.media.media_relevance import local_images_for_gallery_items
        from urbanlens.dashboard.services.media.previews import gallery_urls
        from urbanlens.dashboard.services.media.subject_relevance import subject_for_pin

        profile, _ = Profile.objects.get_or_create(user=request.user)
        relevance = dict(
            MediaRelevance.objects.for_gallery(profile, location, source).values_list("item_key", "is_relevant"),
        )
        kept = {key for key, is_relevant in relevance.items() if is_relevant}
        items = panel.gallery_items(cached.data or {}, subject_for_pin(pin), kept=kept)
        # The remote page_url stays the "Open source" link regardless, so the original is never lost.
        local_images = local_images_for_gallery_items(location, source, [item.url for item in items])
        pictures = gallery_urls(items, provider=source)
        rendered_items = [
            {
                "item": item,
                "key": media_item_key(item.url),
                "is_relevant": relevance.get(media_item_key(item.url)),
                "local_url": local_images[item.url].file_url if item.url in local_images else None,
                "local_thumb_url": local_images[item.url].thumb_url if item.url in local_images else None,
                # TIFFs, scanned PDFs and HEICs reach the gallery routinely and
                # none of them render in an <img> - see services.media.previews.
                "thumb_url": picture.thumb,
                "view_url": picture.view,
            }
            for item, picture in zip(items, pictures, strict=True)
        ]

        # Render even when a provider found nothing, so admins can see what was searched (including every
        # candidate query tried) in the debug overlay rather than the request silently vanishing as a 204.
        context = {
            "rendered_items": rendered_items,
            "source_key": source,
            "debug": self._debug_entry(request, source, cached.query_key, from_cache=True, count=len(items)),
        }
        return render(request, "dashboard/partials/pins/pin_media_items.html", context)

    def _photos_media_preview(self, request: HttpRequest, pin_slug: str):
        """Render the pin owner's own most-recent photos as Media-gallery tiles.

        A lightweight, read-only preview (view + open in the lightbox; no relevance marking, since that
        concept doesn't apply to your own upload) feeding the combined Media section's default "All" view
        alongside the external providers - full management (delete, reposition, cover photo, bulk actions,
        unlimited pagination) lives in that section's "Mine" tab, which reuses the pin gallery panel
        (``image_gallery.PinGalleryView``) completely unchanged.

        Args:
            request: The current request.
            pin_slug: The pin's slug, from the URL kwargs.

        Returns:
            The rendered ``pin_media_items.html`` fragment, or 204 when the pin has no photos of its own
            yet.
        """
        from django.db.models import F, Q

        from urbanlens.dashboard.models.images.model import Image
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        try:
            pin = Pin.objects.get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        # Most-likely-relevant first (REData's cached confidence - see services.photos.redata_relevance),
        # falling back to upload order for a photo REData hasn't scored yet (no location at submission time,
        # REData not configured, or the score just hasn't landed).
        images = Image.objects.filter(pin=pin, profile=profile).filter(Q(media_source_key="") | Q(media_source_key__isnull=True)).exclude(image="").order_by(F("redata_confidence").desc(nulls_last=True), "-created")[:_MEDIA_PHOTOS_PREVIEW_LIMIT]

        rendered_items = [
            {
                # A photo still being processed names no file; the tile is a placeholder until it settles.
                "item": MediaItem(url=img.display_url, thumb_url=img.thumb_url, caption=img.caption or "", source="My Photos", page_url=img.display_url, author=img.author or ""),
                "thumb_url": img.thumb_url,
                "processing": ("failed" if img.processing_failed else "pending") if img.pending_scan else "",
                "processing_failed": img.processing_failed,
                "key": f"photo-{img.pk}",
                "is_relevant": None,
                "image_id": img.pk,
                "lat": img.latitude,
                "lng": img.longitude,
                # Always true - this preview is already scoped to the viewer's own pin (see the queryset above)
                # - but set explicitly rather than left absent, so pin_media_items.html's data-mine reads the
                # same way regardless of which page rendered the tile.
                "is_mine": True,
                "copied_from_label": img.copied_from_label or "",
            }
            for img in images
        ]
        if not rendered_items:
            return HttpResponse(status=204)

        context = {
            "rendered_items": rendered_items,
            "source_key": "photos",
            "debug": self._debug_entry(request, "photos", "own uploads", from_cache=False, count=len(rendered_items)),
        }
        return render(request, "dashboard/partials/pins/pin_media_items.html", context)

    @action(detail=True, methods=["post"])
    def media_relevance(self, request: Request, pin_slug: str):
        """Set (or clear) the requesting user's relevance mark on one Media gallery item.

        An optional ``latitude``/``longitude`` pair (sent when the item was dragged onto the map rather than
        clicked "relevant") is applied to the materialized ``Image`` in the same request, so a freshly
        materialized photo never has a moment with no coordinates.

        A ``hidden`` flag instead of ``is_relevant`` removes the item from this user's results, or restores it: a
        private mark that is no vote, so no one else's gallery or score changes.
        """
        from urbanlens.dashboard.models.images.relevance import ITEM_KEY_LENGTH, MediaRelevance, media_item_key
        from urbanlens.dashboard.services.media.images import coerce_coordinates
        from urbanlens.dashboard.services.media.media_materialize import find_materialized_image
        from urbanlens.dashboard.services.media.media_relevance import record_relevant_and_cache
        from urbanlens.dashboard.services.photos.redata_relevance import queue_relevance_vote

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found."}, status=404)
        if not pin.location:
            return JsonResponse({"error": "Pin has no location."}, status=400)

        try:
            data = drf_data_object(request)
            source = str(data["source"])[:30]
            url = str(data["url"])
            is_relevant = data.get("is_relevant")
            page_url = str(data.get("page_url") or "")
            caption = str(data.get("caption") or "")
        except (KeyError, ValueError, TypeError, ParseError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        coordinates = None
        if "latitude" in data or "longitude" in data:
            try:
                coordinates = coerce_coordinates(data)
            except ValueError as exc:
                # coerce_coordinates() raises one of a fixed set of developer-authored literals; match rather
                # than echo exc so a future raise site added there can't leak unsafe text here.
                logger.info("coerce_coordinates rejected input: %s", exc)
                if str(exc) == "Coordinates must be finite numbers.":
                    return JsonResponse({"error": "Coordinates must be finite numbers."}, status=400)
                if str(exc) == "Coordinates out of range.":
                    return JsonResponse({"error": "Coordinates out of range."}, status=400)
                return JsonResponse({"error": "Invalid request data."}, status=400)

        item_key = data.get("item_key") or media_item_key(url)
        if not isinstance(item_key, str) or len(item_key) > ITEM_KEY_LENGTH:
            return JsonResponse({"error": "Invalid request data."}, status=400)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        if "hidden" in data:
            hidden = data["hidden"]
            if not isinstance(hidden, bool):
                return JsonResponse({"error": "Invalid request data."}, status=400)
            if hidden:
                MediaRelevance.objects.update_or_create(profile=profile, location=pin.location, source=source, item_key=item_key, defaults={"is_relevant": False, "is_vote": False})
            else:
                MediaRelevance.objects.for_gallery(profile, pin.location, source).filter(item_key=item_key, is_vote=False).delete()
            return JsonResponse({"hidden": hidden})

        if is_relevant is None:
            MediaRelevance.objects.for_gallery(profile, pin.location, source).filter(item_key=item_key).delete()
            return JsonResponse({"is_relevant": None})

        response: dict = {"is_relevant": bool(is_relevant)}
        if is_relevant:
            # An explicit click overrides any prior vote for this profile.
            result = record_relevant_and_cache(
                location=pin.location,
                profile=profile,
                source=source,
                url=url,
                page_url=page_url,
                caption=caption,
                pin=pin,
                item_key=item_key,
            )
            if result.error:
                response["materialize_error"] = result.error
            elif result.image is not None:
                image = result.image
                response["image_id"] = image.pk
                response["image_url"] = image.file_url
                if coordinates is not None:
                    image.latitude, image.longitude = coordinates
                    image.save(update_fields=["latitude", "longitude"])
                    response["latitude"] = float(image.latitude)
                    response["longitude"] = float(image.longitude)
        else:
            MediaRelevance.objects.update_or_create(
                profile=profile,
                location=pin.location,
                source=source,
                item_key=item_key,
                defaults={"is_relevant": False, "is_vote": True},
            )
            # Marking "not relevant" never materializes a new copy - but if this item was already saved (e.g. an
            # earlier "relevant" vote, or a wiki send), REData should hear about the reversal too.
            existing_image = find_materialized_image(pin.location, source, url, page_url=page_url, pin=pin, profile=profile)
            if existing_image is not None:
                queue_relevance_vote(existing_image, profile, is_relevant=False)
        return JsonResponse(response)

    @action(detail=True, methods=["post"])
    def media_send_to_wiki(self, request: Request, pin_slug: str):
        """Queue selected Media gallery items for attachment to this location's wiki.

        The download itself runs in ``tasks.cache_media_item_into_wiki`` - see there
        for why a selection of up to 20 is not fetched inside the request.
        """
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import cache_media_item_into_wiki

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found."}, status=404)
        if not pin.location:
            return JsonResponse({"error": "Pin has no location."}, status=400)

        wiki = Wiki.objects.get_for_location(pin.location)
        if wiki is None:
            return JsonResponse({"error": "Create a community wiki for this location first."}, status=400)

        try:
            data = drf_data_object(request)
            items = data["items"]
        except (KeyError, TypeError, ParseError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        # Enqueued rather than downloaded here: a full selection is up to 20 remote fetches, which inside the
        # request is a multi-second hang with no progress indicator, and a request that times out partway
        # attaches some photos and drops the rest with nothing said.
        queued = 0
        errors: list[str] = []
        for entry in items[:20]:
            try:
                url = str(entry["url"])
                source = str(entry.get("source", ""))[:30]
                page_url = str(entry.get("page_url") or "")
                caption = str(entry.get("caption") or "")
            except (KeyError, TypeError, ValueError):
                logger.warning("media_send_to_wiki: malformed item entry: %r", entry)
                errors.append("Could not save this photo.")
                continue
            safely_enqueue_task(cache_media_item_into_wiki, wiki.pk, profile.pk, source, url, page_url, caption)
            queued += 1

        return JsonResponse({"queued": queued, "errors": errors})

    @action(detail=True, methods=["get"])
    def nearby_pins_json(self, request: Request, pin_slug: str):
        """Return the profile's other pins near this one, for the "Nearby Pins" map layer.

        Off by default on the Private Pin page map - only fetched once the
        user turns the layer on.
        """
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found."}, status=404)
        if not pin.location:
            return JsonResponse({"pins": []})

        labels = Label.objects.exclude(kind=KIND_USER).with_customizations_for(pin.profile).order_by("-order", "name")
        nearby = Pin.objects.filter(profile=pin.profile).exclude(pk=pin.pk).near_point(pin.location.point, radius_km=5).select_related("location", "location__wiki").prefetch_related(Prefetch("labels", queryset=labels))[:200]
        return JsonResponse({"pins": [p.to_detail_json() for p in nearby]})

    @action(detail=False, methods=["post"])
    def set_media_sort(self, request: Request):
        """Persist the requesting user's Media gallery sort-order preference."""
        try:
            data = drf_data_object(request)
            sort = data.get("sort")
        except ParseError:
            return JsonResponse({"error": "Invalid request data."}, status=400)
        if sort not in ("relevant", "recent"):
            return JsonResponse({"error": "Invalid sort value."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.media_gallery_sort = sort
        profile.save(update_fields=["media_gallery_sort", "updated"])
        return JsonResponse({"sort": sort})

    def set_map_height(self, request: Request):
        """Persist the requesting user's dragged Private Pin page map height (px).

        Applies to every Private Pin page's map going forward, not just the one being viewed when the drag
        happened - it's a display preference, not per-pin data.
        """
        try:
            raw_height = drf_data_object(request).get("height")
        except ParseError:
            return JsonResponse({"error": "Invalid request data."}, status=400)
        if raw_height is None:
            return JsonResponse({"error": "Invalid height value."}, status=400)
        try:
            height = int(raw_height)
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid height value."}, status=400)
        height = max(_MAP_HEIGHT_MIN_PX, min(_MAP_HEIGHT_MAX_PX, height))

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.pin_detail_map_height = height
        profile.save(update_fields=["pin_detail_map_height", "updated"])
        return JsonResponse({"height": height})

    def web_search(self, request: HttpRequest, pin_slug):
        """
        Returns the web search results for a pin.
        """
        return self._web_search_response(request, pin_slug, force_refresh=False)

    @action(detail=True, methods=["post"])
    def web_search_refresh(self, request: HttpRequest, pin_slug):
        """
        HTMX partial: force a fresh web search, bypassing the shared cache.

        Only allowed once the cached results are at least
        ``_WEB_SEARCH_MIN_REFRESH_AGE`` old, so the refresh button can't be
        used to burn through search-API quota faster than a real cache miss
        would.
        """
        return self._web_search_response(request, pin_slug, force_refresh=True)

    def _web_search_response(self, request: HttpRequest, pin_slug: str, *, force_refresh: bool):
        from urbanlens.dashboard.services.search.pin_web_search import annotate_results, cached_web_searches, merged_results, pin_web_searches, store_web_search

        try:
            pin: Pin = Pin.objects.select_related("location", "profile").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # The place's own name is enough. Requiring an official name dropped pins whose
        # title came from the owner or from Wikipedia, and the panel then vanished on 204.
        searches = pin_web_searches(pin)
        if not searches:
            if request.GET.get("surface") == "article":
                return render(request, "dashboard/pages/location/web_search.html", {"pin": pin, "search_results": [], "page_obj": None})
            return HttpResponse("", status=204)
        search_name = " || ".join(search.query for search in searches)

        article_surface = request.GET.get("surface") == "article"
        search_allowed = user_has_feature(request.user, SiteFeature.SEARCH)
        if not search_allowed and not article_surface:
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {"pin": pin, "error": "Web search is available to VIP subscribers."},
                status=403,
            )

        location = pin.location
        # The shared search is read by every pin at this Location; one built from this pin's own names only by pins
        # with exactly the same names. A row is a hit only for the query that produced it.
        cached = cached_web_searches(location, searches) if location else {}
        answered = len(cached) == len(searches)

        can_refresh = answered and timezone.now() - max(row.updated for row in cached.values()) >= _WEB_SEARCH_MIN_REFRESH_AGE

        if force_refresh:
            if not can_refresh:
                return HttpResponse("Search results were cached too recently to refresh.", status=429)
            cached = {}
            answered = False

        if answered:
            results = merged_results((cached[search.scope.audience].data or {}).get("results", []) for search in searches)
            if not results and not article_surface:
                return HttpResponse("", status=204)
            page_obj = get_page(request, results, _WEB_SEARCH_PAGE_SIZE)
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {
                    "pin": pin,
                    "search_results": _with_local_images(page_obj.object_list),
                    "page_obj": page_obj,
                    "adaptive_pagination": True,
                    "can_refresh": can_refresh,
                    **self._ai_extract_context(request, pin),
                    "debug": self._debug_entry(request, "web_search", search_name, from_cache=True, count=len(results)),
                },
            )

        if not search_allowed:
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {"pin": pin, "error": "Web search is available to VIP subscribers."},
            )

        from urbanlens.dashboard.services.core.timeout_utils import EXTERNAL_CALL_DEADLINE, call_with_deadline

        answers: dict[str, list[dict]] = {audience: (row.data or {}).get("results", []) for audience, row in cached.items()}
        for search in searches:
            if search.scope.audience in answers:
                continue
            try:
                # Deadline-bounded: this is the one external fetch still made on the request path (interactive,
                # VIP-gated, and cached below), so a slow search backend degrades to the error card instead of
                # holding the request open. search_web() tries every configured provider in priority order, so one
                # unconfigured/rate-limited provider doesn't fail the whole request.
                search_results = call_with_deadline(
                    lambda query=search.query: search_web(query),
                    timeout=EXTERNAL_CALL_DEADLINE,
                    default=None,
                    name="web_search",
                )
                if search_results is None:
                    return render(
                        request,
                        "dashboard/pages/location/web_search.html",
                        {"pin": pin, "error": "Search unavailable. Please try again later."},
                    )
            except (OSError, ValueError, RuntimeError, RequestCancelledError) as e:
                logger.exception("Unable to contact web search API: %s", e)
                return render(
                    request,
                    "dashboard/pages/location/web_search.html",
                    {"pin": pin, "error": "Search unavailable. Please try again later."},
                )
            answers[search.scope.audience] = annotate_results(search_results)
            if location:
                store_web_search(location, search, answers[search.scope.audience])

        search_results = merged_results(answers[search.scope.audience] for search in searches)
        if not search_results and request.GET.get("surface") != "article":
            return HttpResponse("", status=204)

        page_obj = get_page(request, search_results, _WEB_SEARCH_PAGE_SIZE)
        return render(
            request,
            "dashboard/pages/location/web_search.html",
            {
                "pin": pin,
                "search_results": _with_local_images(page_obj.object_list),
                "page_obj": page_obj,
                "adaptive_pagination": True,
                "can_refresh": False,
                **self._ai_extract_context(request, pin),
                "debug": self._debug_entry(request, "web_search", search_name, from_cache=False, count=len(search_results)),
            },
        )

    def _render_media_carousel(
        self,
        request: HttpRequest,
        pin_slug: str,
        *,
        service_key: str,
        collector: Callable[[float, float], tuple[list[_SlideT], list[ProviderFetchResult]]],
        template_name: str,
        deadline_name: str,
        extra_context: dict[str, object] | None = None,
    ) -> HttpResponse:
        """Shared flow behind the satellite and street-view multi-source carousels.

        Both carousels merge several external providers into one slide list behind the same
        warm-cache-then-render-with-a-deadline flow; this is the one piece of that flow the generic
        single-source ``panel_info`` dispatch doesn't already cover, since a carousel combines multiple
        providers' slides rather than rendering one source's own template.

        Args:
            request: The current request.
            pin_slug: The pin's slug, from the URL kwargs.
            service_key: The ``external_data.panel_sources()`` key gating readiness.
            collector: Fetches this carousel's slides for a given (lat, lng).
            template_name: The fragment template to render.
            deadline_name: Label for the deadline-guarded collector call.
            extra_context: Extra template context beyond slides/pin/debug_entries.

        Returns:
            The rendered carousel fragment, a pending-panel placeholder, or a 404 if the pin doesn't belong
            to the requesting user.
        """
        from urbanlens.dashboard.services.core.timeout_utils import EXTERNAL_CALL_DEADLINE, call_with_deadline
        from urbanlens.dashboard.services.pins.external_data import panel_sources

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            # effective_latitude/longitude are typed float and never actually None (Location.latitude/longitude
            # are non-nullable) - falsiness is the real "never geocoded" sentinel here, matching every other
            # coordinate gate in this file (e.g. nominatim_info, panel_info).
            return render(request, template_name, {"error": "No coordinates available."})

        # First visit for these coordinates: warm every provider's slide cache in a Celery task and let the
        # placeholder poll -- the provider chain is several sequential upstreams and must never run on the
        # request path.
        if not panel_sources()[service_key].is_ready(pin):
            return self._pending_panel(request, pin, service_key)

        # Ready: the same collector now runs against warm per-provider caches, so this is normally instant.
        coord_query = f"{lat:.5f}, {lng:.5f}"
        default: tuple[list[_SlideT], list[ProviderFetchResult]] = ([], [])
        try:
            # call_with_deadline only catches its own timeout internally (see timeout_utils.py) - collector()
            # itself has no surrounding handler here, so an unexpected per-provider exception (vs. the
            # count=0-per-result failures the providers are supposed to report themselves) is caught at this
            # call site instead, preserving the documented "failures surface as count=0 entries" contract rather
            slides, provider_results = call_with_deadline(
                lambda: collector(float(lat), float(lng)),
                timeout=EXTERNAL_CALL_DEADLINE,
                default=default,
                name=deadline_name,
            )
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Unable to collect %s slides for pin: %s", deadline_name, e)
            slides, provider_results = default
        # Failures surface as count=0 entries, matching the old inline behaviour.
        debug_entries = []
        for result in provider_results:
            if entry := self._debug_entry(request, result.service, coord_query, from_cache=result.from_cache, count=result.count):
                debug_entries.append(entry)

        if service_key == "street_view" and not slides:
            return HttpResponse(status=204)

        return render(
            request,
            template_name,
            {"slides": _with_local_slides(slides, service_key), "pin": pin, "debug_entries": debug_entries, **(extra_context or {})},
        )

    def satellite_view_carousell(self, request: HttpRequest, **kwargs):
        """Return a multi-source satellite imagery carousel fragment."""
        from urbanlens.dashboard.services.pins.external_data import collect_satellite_slides

        return self._render_media_carousel(
            request,
            kwargs["pin_slug"],
            service_key="satellite",
            collector=collect_satellite_slides,
            template_name="dashboard/pages/location/satellite_view.html",
            deadline_name="satellite-replay",
        )

    def street_view(self, request: HttpRequest, **kwargs):
        """Return a multi-source street-view carousel fragment."""
        from urbanlens.dashboard.services.pins.external_data import collect_street_view_slides

        return self._render_media_carousel(
            request,
            kwargs["pin_slug"],
            service_key="street_view",
            collector=collect_street_view_slides,
            template_name="dashboard/pages/location/street_view.html",
            deadline_name="street-view-replay",
            extra_context={"google_maps_api_key": settings.google_public_api_key},
        )

    @action(detail=True, methods=["get"])
    def import_form(self, request: HttpRequest):
        """View the import wizard dialog.

        The same wizard powers both the pin importer and the Memories "Import routes & history" flow; only
        the surrounding copy differs.

        Args:
            request: The incoming request.

        Returns:
            The rendered import wizard dialog template.
        """
        profile = Profile.objects.get(user=request.user)
        variant = "memories" if request.GET.get("variant") == "memories" else "pins"
        import_title = "Import Routes & History" if variant == "memories" else "Import Pins"
        return render(
            request,
            "dashboard/pages/location/import/csv.html",
            {
                "form": UploadDataFile(),
                "profile": profile,
                "import_variant": variant,
                "import_title": import_title,
                "import_review_title": "Review Import",
                # can_upload_videos/can_use_ai_features come from the add_feature_access context processor (see
                # settings/base.py), not set explicitly here.
            },
        )

    @action(detail=False, methods=["post"])
    def parse_for_preview(self, request: HttpRequest):
        """Store uploaded files for the sandbox worker to read, and answer with where to follow them.

        Nothing here opens an upload: every format the preview reads is an ``untrusted_parse``
        operation, so the reading happens in ``services.pins.import_preview``'s tasks.

        Returns:
            202 with ``job_id`` and ``status_url``. 400 for an invalid form, 409 while the
            account's previous upload is still being read, 503 when it could not be queued.
        """
        from django.urls import reverse

        from urbanlens.dashboard.services.pins.import_preview import ImportPreviewRefusedError, start_import_preview

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)

        form = UploadDataFile(request.POST, request.FILES)
        if not form.is_valid():
            return JsonResponse({"error": "Invalid form."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            job_id = start_import_preview(profile, form.cleaned_data["upload_files"])
        except ImportPreviewRefusedError as refused:
            return JsonResponse({"error": refused.message}, status=refused.status)
        return JsonResponse({"job_id": job_id, "status_url": reverse("pin.import.preview.status", kwargs={"job_id": job_id})}, status=202)

    def import_preview_status(self, request: HttpRequest, job_id: UUID):
        """Report one of the requesting user's import previews, with its lists, labels and history summary once read."""
        from urbanlens.dashboard.models.labels.model import Label
        from urbanlens.dashboard.services.pins.history_import import describe_preview
        from urbanlens.dashboard.services.pins.import_preview import read_preview

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)
        state = read_preview(request.user.pk, str(job_id))
        if state is None:
            return JsonResponse({"error": "Preview not found or expired."}, status=404)
        result = state.get("result")
        if isinstance(result, dict):
            profile, _ = Profile.objects.get_or_create(user=request.user)
            labels = Label.objects.pin_assignable_by(profile).in_display_order()
            result["labels"] = [{"id": b.id, "name": b.name, "color": b.color or "", "icon": b.icon or "", "kind": b.kind} for b in labels]
            result["history_summary"] = describe_preview(result.get("history") or {}, profile)
        return JsonResponse(state)

    # -- External-data HTMX endpoints -------------------------------------------

    def wikipedia_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Wikipedia article summary for the pin's location.

        Returns an empty 204 when no matching article is found; the client-side
        htmx:afterOnLoad handler removes the loading placeholder on 204.
        """
        from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaPanelSource

        panel = WikipediaPanelSource()
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.location or not panel.gate(pin):
            return HttpResponse(status=204)

        cached = panel.cached_entry(pin)
        if cached is None:
            return self._pending_panel(request, pin, "wikipedia", hide_tab_id="article-subtab-btn-wikipedia")
        data = cached.data or {}
        if not panel.shows(pin, data):
            logger.debug("wikipedia_info: no article found for pin %s at (%s, %s)", pin_slug, redact_coordinate(pin.effective_latitude), redact_coordinate(pin.effective_longitude))
            return HttpResponse(status=204)

        from urbanlens.dashboard.services.media.remote_copies import copy_url

        thumbnail = data.get("thumbnail") or ""
        context = {
            "article": {**data, "thumbnail": copy_url(thumbnail, provider="wikipedia", page_url=data.get("url") or "") if thumbnail else ""},
            "pin": pin,
            **self._ai_extract_context(request, pin),
            "debug": self._debug_entry(request, "wikipedia", cached.query_key, from_cache=True, count=1),
        }
        response = render(request, "dashboard/partials/pins/pin_wikipedia.html", context)
        # Wikipedia's own fetch never writes an alias itself, but it feeds the NameProvider pool the Aliases
        # panel's own backfill (and Nominatim's fetch) read from - if that panel already rendered before this
        # data became available, it needs telling to check again.
        return self._notify_panel_ready(request, response, "pinAliasesChanged")

    def loopnet_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: LoopNet commercial real-estate data for the pin's address.

        Requires a full street address; returns 204 when none is available or
        when the search/scrape produces no results.
        """
        from urbanlens.dashboard.plugins.builtin.loopnet import LoopnetPanelSource

        panel = LoopnetPanelSource()
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        # Requires at least street + city precision to search against.
        if not pin.location or not panel.gate(pin):
            return HttpResponse(status=204)
        address = panel.address(pin)

        cached = panel.cached_entry(pin)
        if cached is None:
            return self._pending_panel(request, pin, "loopnet")
        data = cached.data or {}
        if not panel.shows(pin, data):
            logger.debug("loopnet_info: no listings found for pin %s (address=%r)", pin_slug, address)
            return HttpResponse(status=204)

        context = {
            "result": data,
            "address": address,
            "pin": pin,
            **self._ai_extract_context(request, pin),
            "debug": self._debug_entry(request, "loopnet", cached.query_key, from_cache=True, count=len(data.get("listings") or [])),
        }
        return render(request, "dashboard/partials/pins/pin_loopnet.html", context)

    def yelp_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Yelp business details (rating, price, hours, most recent
        review) for the pin's location, found by coordinates/address only.

        Requires a Yelp Fusion API key. Shares its LocationCache row with the
        Media gallery's "yelp" photo tab (see plugins.builtin.yelp.YelpPanelSource) -
        whichever loads first populates it for both.
        """
        from urbanlens.dashboard.plugins.builtin.yelp import YelpPanelSource

        panel = YelpPanelSource()

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.location or not panel.gate(pin):
            return HttpResponse(status=204)

        cached = panel.cached_entry(pin)
        if cached is None:
            return self._pending_panel(request, pin, "yelp")
        data = cached.data or {}
        if not panel.shows(pin, data):
            return HttpResponse(status=204)

        reviews = data.get("reviews") or []
        context = {
            "business": data["business"],
            "latest_review": reviews[0] if reviews else None,
            "debug": self._debug_entry(request, "yelp", cached.query_key, from_cache=True, count=1),
        }
        return render(request, "dashboard/partials/pins/pin_yelp.html", context)

    def nps_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: National Park Service information for the pin's location.

        Shows the nearest NPS unit to the pin's coordinates within REData's
        local-catalog search radius (a proximity search, not strict boundary
        containment - see ``plugins.builtin.nps``). Requires REData to be
        configured.
        """
        from urbanlens.dashboard.plugins.builtin.nps import NpsPanelSource

        panel = NpsPanelSource()
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.location or not panel.gate(pin):
            return HttpResponse(status=204)

        cached = panel.cached_entry(pin)
        if cached is None:
            return self._pending_panel(request, pin, "nps")
        data = cached.data or {}
        if not panel.shows(pin, data):
            logger.debug("nps_info: pin %s is not within any NPS unit", pin_slug)
            return HttpResponse(status=204)

        from urbanlens.dashboard.plugins.builtin.nps import alert_facts, facility_facets_visible, park_facts

        # The same rows the API serves, from the same helpers - the two rendered different subsets of this
        # payload by hand before, and the hours the template did have it declined to read ("Standard hours vary
        # - check NPS.gov", printed over the cached hours).
        show_facility_facets = facility_facets_visible(data, pin)
        context = {
            "park": data,
            "alerts": alert_facts(data, show_facility_facets=show_facility_facets),
            "facts": park_facts(data, show_facility_facets=show_facility_facets),
            "debug": self._debug_entry(request, "nps", cached.query_key, from_cache=True, count=1),
        }
        return render(request, "dashboard/partials/pins/pin_nps.html", context)

    def location_data_overview(self, request: HttpRequest, pin_slug: str):
        """HTMX partial: the Location Data card's Overview, merging what each of its tabs knows about the place.

        See :meth:`_card_overview`.
        """
        from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, PanelPlacement, get_panel_source, tabbed_panels

        pin = self._overview_pin(request, pin_slug)
        if isinstance(pin, HttpResponse):
            return pin
        tabs: list[LocationCachePanelSource] = [source for key in _LOCATION_DATA_BESPOKE_KEYS if isinstance(source := get_panel_source(key), LocationCachePanelSource)]
        tabs += tabbed_panels(_visible_panel_sources(request), PanelPlacement.LOCATION)
        return self._card_overview(request, pin, [(source.key, [source]) for source in tabs], overview_id="location-data-overview-body")

    def property_records_overview(self, request: HttpRequest, pin_slug: str):
        """HTMX partial: the Property Records card's Overview - owner, parcel, year built, historic status and register number.

        See :meth:`_card_overview`. A tab showing other panels inside it (Historic Preservation) is summarised from all of them.
        """
        from urbanlens.dashboard.services.pins.external_data import panels_shown_in

        pin = self._overview_pin(request, pin_slug)
        if isinstance(pin, HttpResponse):
            return pin
        visible = _visible_panel_sources(request)
        tabs: list[tuple[str, list[LocationCachePanelSource]]] = [(tab.key, [tab, *panels_shown_in(tab, visible)]) for tab in _property_tab_sources(visible, pin)]
        return self._card_overview(request, pin, tabs, overview_id="property-records-overview-body")

    @staticmethod
    def _overview_pin(request: HttpRequest, pin_slug: str) -> Pin | HttpResponse:
        """The viewer's pin an Overview describes, or the response for one it cannot.

        Args:
            request: The current request.
            pin_slug: The pin's slug.

        Returns:
            The pin, a 404 for someone else's, or a 204 for one with no place to describe.
        """
        try:
            pin = Pin.objects.select_related("location", "profile__user").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)
        if not pin.location or not pin.effective_latitude or not pin.effective_longitude:
            return HttpResponse(status=204)
        return pin

    def _card_overview(self, request: HttpRequest, pin: Pin, tabs: Sequence[tuple[str, Sequence[LocationCachePanelSource]]], *, overview_id: str) -> HttpResponse:
        """A tabbed card's Overview: each tab's :meth:`~LocationCachePanelSource.overview_summary`, merged without attribution.

        Schedules a fetch for any source without fresh data, renders what is ready and keeps polling while anything is
        pending. Names, in an ``HX-Trigger`` event, the tabs whose sources all settled with nothing to show, so the page
        can remove them rather than leave them to land on "No data available." A source whose gate refuses the pin is
        neither fetched nor shown, as its own tab would not be.

        Args:
            request: The current request.
            pin: The pin being viewed.
            tabs: Each tab's key and the sources it shows, in tab order.
            overview_id: The Overview body's DOM id.

        Returns:
            The merged summary, its pending placeholder, or a 204 when nothing is known.
        """
        from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, InfoPanelSource, cached_entries, gate_allows, schedule_panel_fetch

        sources = list(dict.fromkeys(source for _key, members in tabs for source in members))
        applicable = [source for source in sources if gate_allows(source, pin)]
        entries = cached_entries(pin, applicable)
        empty = {source.key for source in sources if source not in applicable}

        heading_name: str | None = None
        chips: list[str] = []
        fields: list[dict] = []
        notes: list[dict] = []
        footer_links: list[dict] = []
        seen_fields: set[tuple[str, str]] = set()
        seen_footer_urls: set[str] = set()
        pending_any = False
        for source in applicable:
            cached = entries[source.key]
            if cached is None:
                pending_any = schedule_panel_fetch(source.key, pin) or pending_any
                continue
            data = cached.data or {}
            piece = source.overview_summary(pin, data)
            if piece is None:
                # Nothing for the Overview; the tab itself may still have something to show.
                if not (isinstance(source, InfoPanelSource) and source.render_context(pin, data) is not None):
                    empty.add(source.key)
                continue
            if heading_name is None and piece.heading_name:
                heading_name = piece.heading_name
            chips.extend(chip for chip in piece.chips if chip not in chips)
            for summary_field in piece.fields:
                field_key = (summary_field["label"], summary_field["value"])
                if field_key not in seen_fields:
                    seen_fields.add(field_key)
                    fields.append(summary_field)
            tab_label = source.label if isinstance(source, InfoPanelSource) else source.title
            notes.extend({"text": note, "tab_key": source.key, "tab_label": tab_label} for note in piece.notes if all(note != seen["text"] for seen in notes))
            footer_link = piece.footer_link
            if footer_link and footer_link["url"] not in seen_footer_urls:
                seen_footer_urls.add(footer_link["url"])
                footer_links.append(footer_link)

        empty_tabs = [key for key, members in tabs if all(member.key in empty for member in members)]
        attempt = self._poll_attempt(request)
        still_waiting = pending_any and attempt < MAX_POLL_ATTEMPTS

        if not (heading_name or chips or fields or notes or footer_links):
            if still_waiting:
                response = render(
                    request,
                    "dashboard/partials/pins/panel_pending.html",
                    {
                        "section_id": overview_id,
                        "outer_class": "pin-plugin-tab-body",
                        "outer_is_card": True,
                        "icon": "travel_explore",
                        "title": "Overview",
                        "poll_url": request.path,
                        "next_attempt": attempt + 1,
                        "poll_interval": POLL_INTERVAL_SECONDS,
                        "in_tab": True,
                    },
                )
            else:
                response = HttpResponse(status=204)
            return self._notify_empty_tabs(response, empty_tabs)

        # Render whatever's ready now rather than waiting on the slowest source; while anything is pending the body
        # keeps self-polling (an outerHTML swap, as panel_pending.html does) to pick up later arrivals.
        context: dict = {"overview_id": overview_id, "heading_name": heading_name, "chips": chips, "fields": fields, "notes": notes, "footer_links": footer_links}
        if still_waiting:
            context.update({"poll_url": request.path, "next_attempt": attempt + 1, "poll_interval": POLL_INTERVAL_SECONDS})
        response = render(request, "dashboard/partials/pins/_pin_card_overview.html", context)
        return self._notify_empty_tabs(response, empty_tabs)

    @staticmethod
    def _notify_empty_tabs(response: HttpResponse, empty_keys: list[str]) -> HttpResponse:
        """Attach an HX-Trigger event naming the tabs confirmed to have no content.

        Args:
            response: The response to annotate.
            empty_keys: Keys of tabs whose sources all settled with nothing to show - safe to remove.

        Returns:
            The same response, for chaining.
        """
        if not empty_keys:
            return response
        response["HX-Trigger"] = json.dumps({"pinTabsEmpty": {"keys": empty_keys}})
        return response

    def nominatim_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: OpenStreetMap Nominatim place metadata for the pin's location.

        Only renders when at least one useful metadata field is present (website,
        phone, opening hours, operator, or a Wikipedia cross-link).  Returns 204
        for coordinate-only results with no enrichment.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            logger.debug("nominatim_info: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            logger.debug("nominatim_info: pin %s has no coordinates, skipping", pin_slug)
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "nominatim")
        if cached is None:
            return self._pending_panel(request, pin, "nominatim", in_tab=True)
        data = cached.data or None

        useful_fields = ("website", "phone", "email", "opening_hours", "operator", "wikipedia", "wikidata", "image", "extra_details", "kind_label")
        if not data or not any(data.get(k) for k in useful_fields):
            logger.debug("nominatim_info: no enrichment data for pin %s at (%s, %s)", pin_slug, redact_coordinate(lat), redact_coordinate(lng))
            return HttpResponse(status=204)

        context = {"place": data, "debug": self._debug_entry(request, "nominatim", cached.query_key, from_cache=True, count=1)}
        response = render(request, "dashboard/partials/pins/pin_nominatim.html", context)
        # NominatimPanelSource.fetch() can auto-add an OSM link and, via
        # update_location_name_from_external_sources, an alias and/or the pin's own displayed name - tell every
        # panel that could show that.
        return self._notify_panel_ready(request, response, "pinAliasesChanged", "pinLinksChanged", "pinOverviewChanged")

    def azure_maps_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Azure Maps reverse-geocoded address and nearest-POI details for the pin's location.

        Only renders when the payload carries a formatted address or a nearby POI - a
        coordinate-only result (nothing geocoded, nothing nearby) returns 204.
        """
        from urbanlens.dashboard.plugins.builtin.azure_maps import AzureMapsPanelSource

        panel = AzureMapsPanelSource()
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.location or not panel.gate(pin):
            return HttpResponse(status=204)

        cached = panel.cached_entry(pin)
        if cached is None:
            return self._pending_panel(request, pin, "azure_maps")
        data = cached.data or {}
        if not panel.shows(pin, data):
            logger.debug("azure_maps_info: no enrichment data for pin %s at (%s, %s)", pin_slug, redact_coordinate(pin.effective_latitude), redact_coordinate(pin.effective_longitude))
            return HttpResponse(status=204)

        context = {"place": data, "debug": self._debug_entry(request, "azure_maps", cached.query_key, from_cache=True, count=1)}
        return render(request, "dashboard/partials/pins/pin_azure_maps.html", context)

    def _ai_extract_context(self, request: HttpRequest, pin: Pin) -> dict:
        """Context for the AI extract buttons on this pin's external links.

        Single source shared by every panel render path (generic ``panel_info`` dispatch plus the bespoke
        Wikipedia/LoopNet/web-search panels), so the buttons exist-or-don't - and honor the same per-link
        cooldown - consistently across the whole detail page.

        Args:
            request: The current request (viewer is always the pin's owner here).
            pin: The pin being rendered.

        Returns:
            ``{"can_ai_extract": bool, "recently_extracted_urls": frozenset[str]}``, ready to merge into a
            render context...
        """
        from urbanlens.dashboard.services.ai.link_extraction import ai_extract_button_context

        return ai_extract_button_context(request.user, pin.profile, pin)

    def child_details(self, request: HttpRequest, pin_slug: str):
        """HTMX: turn "child pin details" on or off without reloading the page.

        Responds with each panel that reads the setting as an out-of-band swap, loading afresh, and tells the map
        through ``HX-Trigger`` (``childDetailsChanged``) so it refetches its own layers.
        """
        from django.urls import reverse

        try:
            pin = Pin.objects.only("pk", "slug", "uuid").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)
        include_children = request.GET.get("children") == "1"
        response = render(
            request,
            "dashboard/partials/pins/_child_details_regions.html",
            {
                "pin": pin,
                "include_children": include_children,
                "page_url": reverse("pin.details", kwargs={"pin_slug": pin.slug or pin.uuid}),
                "child_details_url": reverse("pin.child_details", kwargs={"pin_slug": pin.slug or pin.uuid}),
            },
        )
        response["HX-Trigger"] = json.dumps({"childDetailsChanged": {"include": include_children}})
        return response

    def parcel_buildings(self, request: HttpRequest, pin_slug: str):
        """HTMX partial: every building standing on this pin's property, and every child pin it has.

        One list (P172): the property's buildings from REData, OpenStreetMap and CRIS, each marked and opened in place
        when a child pin covers it, and a Child pins tab with every child of any type. A pin the Buildings list does not
        apply to (a child pin, one with no coordinates) still lists its own children.
        """
        from django.urls import reverse

        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.models.pin.model import PinType
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import child_pin_rows, match_buildings_to_children, unpinned_building_child_rows
        from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
        from urbanlens.dashboard.services.pins.external_data import get_panel_source
        from urbanlens.dashboard.services.pins.pin_restructure import missing_buildings, property_polygon

        try:
            pin = Pin.objects.select_related("location", "profile").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        def url_for(child: Pin) -> str:
            return reverse("pin.details", kwargs={"pin_slug": child.slug or child.uuid})

        children = list(pin.detail_pins.select_related("location__wiki"))
        child_rows = child_pin_rows(children, url_for=url_for)
        panel = get_panel_source(PARCEL_BUILDINGS_CACHE_SOURCE)
        rows: list[dict] = []
        unpinned_count = 0
        debug = None
        cached = None
        if panel is not None and panel.gate(pin):
            cached = LocationCache.get_fresh(pin.location, PARCEL_BUILDINGS_CACHE_SOURCE)
            if cached is None:
                pending = self._pending_panel(request, pin, PARCEL_BUILDINGS_CACHE_SOURCE)
                # Building data that will never arrive must not hide the child pins.
                if pending.status_code != 204 or not child_rows:
                    return pending
        if cached is not None:
            buildings = (cached.data or {}).get("buildings") or []
            descendants = list(pin.descendants().select_related("location__wiki"))
            external_rows, unmatched = match_buildings_to_children(buildings, descendants, url_for=url_for, boundary_polygon=property_polygon(pin))
            if any(not row["child_uuid"] for row in external_rows):
                from urbanlens.dashboard.services.pins.auto_nest import request_sweep

                request_sweep(pin)
            rows = external_rows + unpinned_building_child_rows(unmatched, url_for=url_for)
            # From the import's own view of the parcel, not from the rows: the button must promise exactly what
            # pressing it will do.
            unpinned_count = len(missing_buildings(pin))
            debug = self._debug_entry(request, PARCEL_BUILDINGS_CACHE_SOURCE, cached.query_key, from_cache=True, count=len(rows))
            # A child covering a building record stands for that building, whatever type it is stored as.
            building_slugs = {marker.slug for marker in descendants if marker.pin_type == PinType.BUILDING and marker.slug} | {row["child_slug"] for row in external_rows if row["child_slug"]}
        else:
            building_slugs = {child.slug for child in children if child.pin_type == PinType.BUILDING and child.slug}
        if not rows and not child_rows:
            return HttpResponse(status=204)

        for row in (*rows, *child_rows):
            row["opens_in_place"] = row.get("child_slug") in building_slugs
        return render(
            request,
            "dashboard/partials/pins/_parcel_buildings_panel.html",
            {
                "section_id": "parcel-buildings-section",
                "icon": "apartment" if rows else "account_tree",
                "title": "Buildings on this Property" if rows else "Child pins",
                "pin": pin,
                # Named "rows" to match the key the wiki page's own render of this template uses.
                "rows": rows,
                "child_rows": child_rows,
                "has_wiki": Wiki.objects.get_for_location(pin.location) is not None,
                "unpinned_count": unpinned_count,
                "debug": debug,
            },
        )

    def panel_info(self, request: HttpRequest, pin_slug: str, panel_key: str):
        """
        HTMX partial: generic external-data info panel, dispatched by registered source.

        Backs every ``InfoPanelSource``-based panel (Photon, US Census Geography,
        EPA Regulated Facilities, iNaturalist, News, Building Characteristics,
        Recent Seismic Activity, and any future plugin panel of this shape).
        A plugin ships a new simple info panel by contributing an
        ``InfoPanelSource`` subclass alone - no new route or controller method
        needed. Panels with bespoke markup (Wikipedia, Yelp, NPS, Nominatim,
        Azure Maps, LoopNet, USGS Topo, ...) keep their own dedicated methods.

        A source declaring ``required_feature`` is refused here as well as
        omitted from the page's tab strip - see :func:`_viewer_may_see_panel`.
        """
        return self._render_info_panel(request, pin_slug, panel_key, in_building_card=False)

    def building_panel_info(self, request: HttpRequest, pin_slug: str, panel_key: str):
        """HTMX partial: a ``building_level`` info panel for a building child pin, inside its card on the parent's page.

        Rendered nested, under a DOM id carrying the building's slug, so several buildings' cards and the parent's own
        panels can share one page.
        """
        return self._render_info_panel(request, pin_slug, panel_key, in_building_card=True)

    def _render_info_panel(self, request: HttpRequest, pin_slug: str, panel_key: str, *, in_building_card: bool):
        """Render one info panel for a pin, or its pending placeholder.

        Args:
            request: The current request.
            pin_slug: The pin's slug.
            panel_key: An ``InfoPanelSource`` key.
            in_building_card: Whether the panel sits in a building child's card on its parent's page.

        Returns:
            The panel, its placeholder, a 204 when there is nothing to show, or a 404.
        """
        from urbanlens.dashboard.services.pins.external_data import InfoPanelSource, PanelPlacement, get_panel_source, panel_sources, panels_shown_in

        panel = get_panel_source(panel_key)
        if not isinstance(panel, InfoPanelSource) or (in_building_card and not panel.building_level):
            return HttpResponse(status=404)

        # Refused before the pin is even looked up, so a viewer without the feature can neither read the panel
        # nor (via _pending_panel below) spend an upstream fetch they will never be shown the result of.
        if not _viewer_may_see_panel(request, panel):
            return HttpResponse(status=404)

        try:
            pin = Pin.objects.select_related("location", "profile").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        section_id = f"{panel.section_id}--{pin.slug}" if in_building_card else panel.section_id
        in_tab = not in_building_card and panel.placement != PanelPlacement.STANDALONE
        companions = [source for source in panels_shown_in(panel, panel_sources().values()) if (source.building_level or not in_building_card) and _viewer_may_see_panel(request, source)]
        if companions:
            return self._render_merged_panel(request, pin, [panel, *companions], section_id=section_id, in_tab=in_tab, nested=in_building_card or in_tab)

        if not panel.gate(pin):
            return HttpResponse(status=204)

        cached = panel.cached_entry(pin)
        if cached is None:
            return self._pending_panel(request, pin, panel_key, section_id=section_id, in_tab=in_tab)
        data = cached.data or {}

        context = panel.render_context(pin, data)
        if context is None:
            return HttpResponse(status=204)

        context["section_id"] = section_id
        context["icon"] = panel.icon
        context["title"] = panel.title
        # Decided here rather than taken from render_context: a panel cannot know whether it was rendered into a
        # tab strip (which supplies the card chrome) or standalone (which does not - the placeholder it replaces
        # via hx-swap="outerHTML" takes its card with it).
        context["nested"] = in_building_card or in_tab
        context["debug"] = self._debug_entry(request, panel_key, cached.query_key, from_cache=True, count=panel.debug_count(data))
        # Links a panel marks with ai_extract=True get the AI extraction button.
        context["pin"] = pin
        context.update(self._ai_extract_context(request, pin))

        response = render(request, "dashboard/partials/pins/_simple_info_panel.html", context)
        if panel_key == "epa_echo_detail":
            # EpaEchoDetailPanelSource.fetch() can auto-add a compliance-report link.
            response = self._notify_panel_ready(request, response, "pinLinksChanged")
        return response

    def _render_merged_panel(self, request: HttpRequest, pin: Pin, members: Sequence[InfoPanelSource], *, section_id: str, in_tab: bool, nested: bool) -> HttpResponse:
        """Render a panel and the panels shown inside it as one, each under its own title, a repeated fact given once.

        Each member keeps its own gate, fetch and failure handling: a member still fetching is scheduled and polled for
        while the others show.

        Args:
            request: The current request; its path is polled.
            pin: The pin being viewed.
            members: The host panel first, then the panels it shows.
            section_id: The panel's DOM id.
            in_tab: Whether it fills a tab body.
            nested: Whether its card chrome comes from the surrounding card.

        Returns:
            The merged panel, its pending placeholder, or a 204 when no member has anything to show.
        """
        from urbanlens.dashboard.services.pins.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, cached_entries, gate_allows, schedule_panel_fetch, without_repeats

        host = members[0]
        attempt = self._poll_attempt(request)
        applicable = [member for member in members if gate_allows(member, pin)]
        entries = cached_entries(pin, applicable)
        sections: list[dict] = []
        pending = False
        for member in applicable:
            cached = entries[member.key]
            if cached is None:
                if attempt < MAX_POLL_ATTEMPTS:
                    pending = schedule_panel_fetch(member.key, pin) or pending
                continue
            data = cached.data or {}
            context = member.render_context(pin, data)
            if context is not None:
                debug = self._debug_entry(request, member.key, cached.query_key, from_cache=True, count=member.debug_count(data))
                sections.append({**context, "source_title": member.title, "source_icon": member.icon, "debug": debug})

        if not sections:
            if pending:
                return self._pending_placeholder(request, host, section_id=section_id, in_tab=in_tab, title=host.label)
            return HttpResponse(status=204)

        context = {"section_id": section_id, "icon": host.icon, "title": host.label, "nested": nested, "sections": without_repeats(sections), "pin": pin}
        context.update(self._ai_extract_context(request, pin))
        if pending:
            context.update({"poll_url": request.path, "next_attempt": attempt + 1, "poll_interval": POLL_INTERVAL_SECONDS})
        return render(request, "dashboard/partials/pins/_merged_info_panel.html", context)

    def usgs_topo_info(self, request: HttpRequest, pin_slug: str):
        """HTMX partial: USGS Historical Topographic Map Collection maps near the pin."""
        from urbanlens.dashboard.plugins.builtin.usgs import UsgsTopoPanelSource

        panel = UsgsTopoPanelSource()
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.location or not panel.gate(pin):
            return HttpResponse(status=204)

        cached = panel.cached_entry(pin)
        if cached is None:
            return self._pending_panel(request, pin, "usgs_topo")
        data = cached.data or {}
        if not panel.shows(pin, data):
            logger.debug("usgs_topo_info: no topo maps found for pin %s", pin_slug)
            return HttpResponse(status=204)
        maps_list = data["items"]

        context = {
            "maps": maps_list[:20],
            "debug": self._debug_entry(request, "usgs_topo", cached.query_key, from_cache=True, count=len(maps_list)),
        }
        return render(request, "dashboard/partials/pins/pin_usgs_topo.html", context)

    # Sources rendered via LocationCache on the Private Pin page (see the endpoints above).
    _LOCATION_CACHE_DEBUG_SOURCES = ("wikipedia", "nominatim", "nps", "loopnet", "usgs_topo", "smithsonian", "wikimedia", "library_of_congress", "web_search")
    # Gateway service_keys used by the satellite/street-view carousels (see satellite_view_carousell / street_view).
    _SATELLITE_DEBUG_SERVICES = ("google_maps", "esri", "nasa_gibs", "mapbox", "bing_maps", "open_aerial_map")
    _STREET_VIEW_DEBUG_SERVICES = ("google_maps", "mapillary", "kartaview")

    @action(detail=True, methods=["post"])
    def clear_debug_cache(self, request: HttpRequest, pin_slug: str):
        """
        Clear every cached external-API result shown on this pin's detail page.

        Admin-only (see ``debug_overlay.can_view_debug_overlay``): this busts
        caches in front of rate-limited third-party APIs, so it must not be
        reachable by regular users. Does not clear Esri's global Wayback
        release-list cache, which is shared across all pins/users.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.admin.debug_overlay import can_view_debug_overlay

        if not can_view_debug_overlay(request.user):
            return HttpResponse(status=403)

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        cleared = 0
        if pin.location:
            cleared, _ = LocationCache.objects.filter(
                location=pin.location,
                source__in=self._LOCATION_CACHE_DEBUG_SOURCES,
            ).delete()

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is not None and lng is not None:
            lat_key, lng_key = f"{float(lat):.5f}", f"{float(lng):.5f}"
            for service_key in self._SATELLITE_DEBUG_SERVICES:
                cache.delete(make_cache_key(f"satellite_view_{service_key}", lat_key, lng_key))
            for service_key in self._STREET_VIEW_DEBUG_SERVICES:
                cache.delete(make_cache_key(f"street_view_{service_key}", lat_key, lng_key))

        return JsonResponse({"cleared": cleared})

    @action(detail=False, methods=["post"])
    def import_confirmed(self, request: Request):
        """Queue user-confirmed pin selections from the preview step as a background import.

        Returns:
            202 with ``job_id``, ``total``, ``status_url`` and ``cancel_url``. 400 for a
            selection refused before anything is stored, 409 while the account's
            previous import is still running, 503 when it could not be queued.
        """
        from urbanlens.dashboard.services.pins.confirmed_import import ConfirmedImportRefusedError, start_confirmed_import

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)

        try:
            payload = request.data
            confirmed_lists = payload.get("lists", [])
            auto_tag = bool(payload.get("auto_tag", True))
            preview_id = payload.get("preview_id")
        except (ValueError, KeyError, AttributeError, ParseError):
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            started = start_confirmed_import(profile, confirmed_lists, auto_tag=auto_tag, preview_id=preview_id)
        except ConfirmedImportRefusedError as refused:
            body = {"error": refused.message}
            if refused.job_id:
                body.update(_confirmed_import_urls(refused.job_id))
            return JsonResponse(body, status=refused.status)
        return JsonResponse({"total": started.total, **_confirmed_import_urls(started.job_id)}, status=202)

    def import_confirmed_status(self, request: HttpRequest, job_id: UUID):
        """Report one of the requesting user's confirmed imports, for the import dialog to poll."""
        from urbanlens.dashboard.services.pins.confirmed_import import read_status

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)
        data = read_status(request.user.pk, str(job_id))
        if data is None:
            return JsonResponse({"error": "Import not found or expired."}, status=404)
        return JsonResponse(data)

    def import_confirmed_cancel(self, request: HttpRequest, job_id: UUID):
        """Ask one of the requesting user's confirmed imports to stop."""
        from urbanlens.dashboard.services.pins.confirmed_import import cancel_confirmed_import

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)
        if not cancel_confirmed_import(request.user.pk, str(job_id)):
            return JsonResponse({"error": "Import not found or expired."}, status=404)
        return JsonResponse({"status": "cancelling"}, status=202)

    def weather_forecast(self, request: HttpRequest, pin_slug):
        """
        Returns the weather forecast for a pin.

        Tries REData first when configured (one call covers every registered
        provider); otherwise falls back to OpenWeatherMap when a key is
        configured, then the free, keyless Open-Meteo gateway - see
        ``services.apis.locations.weather_resolution``. Every path renders
        through the same normalized ``ForecastSlot`` shape.
        """
        from urbanlens.dashboard.services.apis.locations.weather_resolution import get_forecast_slots, get_sun_times

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.external_apis_enabled:
            return HttpResponse("External weather lookups are turned off in your settings.", status=403)

        # Get the pin
        try:
            pin: Pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        if not pin.location or not pin.location.latitude or not pin.location.longitude:
            return HttpResponse("Pin does not have valid coordinates", status=400)

        latitude, longitude = float(pin.location.latitude), float(pin.location.longitude)
        forecast = get_forecast_slots(latitude, longitude)
        logger.debug("forecast_data: %s", forecast)
        sun_times = get_sun_times(latitude, longitude)

        return render(request, "dashboard/pages/location/weather.html", {"forecast": forecast, "sun_times": sun_times})


def _confirmed_import_urls(job_id: str) -> dict[str, str]:
    """Where the import dialog follows and cancels a confirmed import.

    Args:
        job_id: The import.

    Returns:
        ``job_id``, ``status_url`` and ``cancel_url``.
    """
    from django.urls import reverse

    return {
        "job_id": job_id,
        "status_url": reverse("pin.import.confirmed.status", kwargs={"job_id": job_id}),
        "cancel_url": reverse("pin.import.confirmed.cancel", kwargs={"job_id": job_id}),
    }


_REDATA_MEDIA_CACHE_TTL = 3600

#: Largest proxied REData body worth putting in the shared Dragonfly. Larger than
#: ``bounded_cache.MAX_CACHED_BODY_BYTES``, deliberately and at the call site:
#: that ceiling is sized for thumbnails, and these are scanned PDFs and TIFFs, so
#: inheriting it would refuse to cache almost all of them and turn every view
#: into a fresh REData download - a different resource spent, not a saving.
REDATA_MEDIA_MAX_CACHED_BYTES = 4 * 1024 * 1024

#: How often one caller may pull these proxies. Charged to the account when there
#: is one and to the address otherwise: an address is a poor identity behind NAT,
#: where an office would share one budget, and a poor isolation boundary, since
#: the requirement is that one account cannot spend everyone else's. Generous
#: either way - a panel is many requests, and a limit tight enough to break
#: ordinary browsing would be reverted rather than tuned.
REDATA_MEDIA_RATE = Rate(limit=600, window_seconds=300)

#: GET is the expensive method here, which the throttle's default set excludes.
REDATA_MEDIA_METHODS = frozenset({"GET"})


class RedataMediaProxyMixin:
    """Shared caching + preview handling for the REData-backed media proxies.

    Each of these views fetches one file's bytes from REData (whose API key must never reach the
    browser) and serves them through ``proxied_media_response``: the routes are unauthenticated and the bytes
    are a third party's, so only an allow-listed type is ever displayed inline.
    The conversion also belongs here rather than behind the generic ``media_preview`` endpoint, which
    would only re-download what this view already has.
    """

    def serve_media(self, request: HttpRequest, cache_key: str, download: Callable[[], tuple[bytes, str]], *, unavailable_errors: tuple[type[Exception], ...] | None = None) -> HttpResponse:
        """Serve one REData file, or a rendering of it a browser can show.

        Args:
            request: The current request. ``?preview=thumb`` asks for a gallery tile's rendering and ``?preview=1`` for
            the lightbox's (an original a browser can already show is served as it is for the lightbox).
            cache_key: Django cache key for the *original* bytes.
            download: Zero-argument callable returning ``(content, content_type)``.
            unavailable_errors: Exception types ``download`` raises to mean "not available" (a 404, an
            unconfigured gateway, ...), each turned into a 404 response...

        Returns:
            The file or its kept rendering; a 503 with ``Retry-After`` while the rendering is being made, while REData is
            throttled, its source is down or this process already has as many REData downloads in flight as it may; a
            502 when the download failed; or a 404 when REData couldn't supply it or it could not be rendered.
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError
        from urbanlens.dashboard.services.apis.request_upstreams import RedataMediaUpstream
        from urbanlens.dashboard.services.core.gateway import UpstreamBusyError
        from urbanlens.dashboard.services.media import proxied_renders
        from urbanlens.dashboard.services.media.previews import PREVIEW_RETRY_AFTER_SECONDS, is_web_safe, needs_server_side_preview
        from urbanlens.dashboard.services.media.proxied_media import inline_media_type, proxied_media_response, retry_later_response

        if unavailable_errors is None:
            unavailable_errors = (PropertyRecordsUnavailableError, ValueError)

        size = request.GET.get("preview", "")
        if size not in proxied_renders.SIZES:
            size = ""
        label = f"REData media {cache_key}"
        if size:
            if (kept := self._serve_kept_render(cache_key, size)) is not None:
                return kept
            if proxied_renders.has_failed(cache_key, size):
                return HttpResponse(status=404)
            if proxied_renders.is_pending(cache_key, size):
                return retry_later_response(PREVIEW_RETRY_AFTER_SECONDS)

        original = get_or_none(cache_key, label=label)
        if original is None:

            def download_and_keep() -> tuple[bytes, str]:
                body = download()
                # In the fetching thread, so a download that outlives the request is still kept for the next one.
                # Refusing to cache never means refusing to answer: what it stops is one oversized document
                # evicting other people's entries from the shared instance.
                set_if_small(cache_key, body[0], body[1], _REDATA_MEDIA_CACHE_TTL, label=label, max_bytes=REDATA_MEDIA_MAX_CACHED_BYTES)
                return body

            fetched = RedataMediaUpstream.call(download_and_keep, errors=unavailable_errors)
            if fetched.value is None:
                if isinstance(fetched.error, UpstreamBusyError):
                    return retry_later_response(fetched.error.retry_after)
                if isinstance(fetched.error, unavailable_errors):
                    return HttpResponse(status=404)
                if fetched.error is not None:
                    return HttpResponse(status=502)
                return retry_later_response(fetched.retry_after or RedataMediaUpstream.busy_retry_seconds)
            original = fetched.value

        content, content_type = original
        # The lightbox shows a displayable original as it is; re-encoding it would only cost quality. A tile still gets
        # a small rendering, since the original may be any size.
        if not size or (size == proxied_renders.VIEW and is_web_safe(request.path, content_type) and inline_media_type(content, content_type) is not None):
            return proxied_media_response(content, content_type)

        declared = content_type.split(";")[0].strip().lower()
        # Pillow tries any image type, so only a known non-image no renderer handles is refused outright.
        if declared not in ("", "application/octet-stream") and not declared.startswith("image/") and not needs_server_side_preview(request.path, declared):
            return HttpResponse(status=404)
        # The decode runs in the sandbox worker, not here - these are a third party's document bytes and
        # render_preview reaches Pillow and poppler.
        proxied_renders.request_render(cache_key, size, original)
        return retry_later_response(PREVIEW_RETRY_AFTER_SECONDS)

    @staticmethod
    def _serve_kept_render(cache_key: str, size: str) -> HttpResponse | None:
        # Through proxied_media_response, not an X-Accel hand-off: nginx would drop the headers it sets.
        from urbanlens.dashboard.services.media import proxied_renders
        from urbanlens.dashboard.services.media.proxied_media import proxied_media_response

        render = proxied_renders.kept(cache_key, size)
        if render is None:
            return None
        try:
            with render.file.open("rb") as stored:
                content = stored.read()
        except OSError:
            logger.warning("Kept rendering %s is missing its file", render.pk)
            render.delete()
            return None
        response = proxied_media_response(content, render.content_type)
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class PinLoopnetPhotoView(RedataMediaProxyMixin, View):
    """GET pin/loopnet/photo/<listing_uuid>/<photo_id>/ - proxies one LoopNet listing photo.

    REData's API key must never reach the browser, so photo bytes are fetched server-side (same
    reasoning as ``PinImmichThumbnailView``) and cached briefly to avoid re-hitting REData on every
    gallery view.
    No login required, unlike the Immich proxy: LoopNet listing photos are public marketing material
    (not a specific user's private library), and
    ``services.media.media_materialize.materialize_media_item`` downloads this same URL server-side with
    no session of its own - it would 302 to the login page and fail if this endpoint required one.
    """

    def get(self, request: HttpRequest, listing_uuid: str, photo_id: int) -> HttpResponse:
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        return self.serve_media(
            request,
            f"ul_loopnet_photo_{listing_uuid}_{photo_id}",
            lambda: RedataGateway().download_listing_photo(listing_uuid, photo_id),
        )


class PinCrisAttachmentView(RedataMediaProxyMixin, View):
    """GET pin/cris/attachment/<resource_uuid>/<attachment_id>/ - proxies one CRIS attachment/photo.

    Same reasoning as ``PinLoopnetPhotoView`` - no login required (CRIS documents/photos are public
    historic-preservation records, and ``materialize_media_item`` needs an unauthenticated URL to
    re-download this from).
    """

    def get(self, request: HttpRequest, resource_uuid: str, attachment_id: int) -> HttpResponse:
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        return self.serve_media(
            request,
            f"ul_cris_attachment_{resource_uuid}_{attachment_id}",
            lambda: RedataGateway().download_cultural_resource_attachment(resource_uuid, attachment_id),
        )


class PinCrisExtractedImageView(RedataMediaProxyMixin, View):
    """GET pin/cris/attachment/<resource_uuid>/<attachment_id>/extracted/<image_id>/ - proxies
    one photo OCR/AI-extracted from a CRIS document attachment.

    Same reasoning as ``PinCrisAttachmentView`` - no login required.
    """

    def get(self, request: HttpRequest, resource_uuid: str, attachment_id: int, image_id: int) -> HttpResponse:
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        return self.serve_media(
            request,
            f"ul_cris_extracted_image_{resource_uuid}_{attachment_id}_{image_id}",
            lambda: RedataGateway().download_extracted_image(resource_uuid, attachment_id, image_id),
        )


class PinPlaceCidMediaView(RedataMediaProxyMixin, View):
    """GET pin/place-cid/media/<cid>/<media_id>/ - proxies one REData deep-scrape media item.

    Same reasoning as ``PinLoopnetPhotoView``/``PinCrisAttachmentView`` - REData's API key must never
    reach the browser - and the same "no login required" call: this is REData's
    ``../REData/docs/api-reference.md`` "GET /places/cid/{cid}/media/{id}/download/" (photos, videos,
    360s, Street View captured for a resolved Google Maps CID), public Google Maps listing media rather
    than anything private to a user, and ``materialize_media_item`` needs an unauthenticated URL to
    re-download it.
    Diverges from those two on the exception it hands ``serve_media``: ``RedataCidGateway`` (this view's
    gateway) raises ``GatewayRequestError`` on failure, not the property-records ``RedataGateway``'s
    ``PropertyRecordsUnavailableError`` - see ``serve_media``'s ``unavailable_errors`` parameter.
    """

    def get(self, request: HttpRequest, cid: int, media_id: int) -> HttpResponse:
        from urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway import RedataCidGateway
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        return self.serve_media(
            request,
            f"ul_place_cid_media_{cid}_{media_id}",
            lambda: RedataCidGateway().download_media(cid, media_id),
            unavailable_errors=(GatewayRequestError, ValueError),
        )
