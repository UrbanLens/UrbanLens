from __future__ import annotations

import base64
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import ParseError
from rest_framework.viewsets import GenericViewSet

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.forms.upload_datafile import UploadDataFile
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.pagination import get_page
from urbanlens.dashboard.services.redact import redact_coordinate
from urbanlens.dashboard.services.search import format_search_date, get_search_gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, StreetViewSlide
    from urbanlens.dashboard.services.external_data import ProviderFetchResult

logger = logging.getLogger(__name__)

_WEB_SEARCH_CLIENT_PAGE_SIZE = 5
_ADAPTIVE_PAGE_BATCH_MULTIPLIER = 2
_WEB_SEARCH_PAGE_SIZE = _WEB_SEARCH_CLIENT_PAGE_SIZE * _ADAPTIVE_PAGE_BATCH_MULTIPLIER
_WEB_SEARCH_MIN_REFRESH_AGE = timedelta(days=1)


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

        from urbanlens.dashboard.models.aliases.model import AliasType
        from urbanlens.dashboard.models.labels.model import COLOR_CHOICES, Label
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.wiki.model import Wiki

        try:
            pin = Pin.objects.select_related("location", "parent_pin", "parent_pin__location").get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            try:
                pin = Pin.objects.select_related("location", "parent_pin", "parent_pin__location").get(uuid=kwargs["pin_slug"], profile__user=request.user)
            except (Pin.DoesNotExist, ValueError):
                return render(
                    request,
                    "dashboard/pages/errors/pin_not_found.html",
                    {"pin_slug": kwargs.get("pin_slug")},
                    status=404,
                )

        pin.backfill_wiki_link_slugs()

        profile, _ = Profile.objects.get_or_create(user=request.user)

        today = date.today()
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

        from urbanlens.dashboard.services.debug_overlay import can_view_debug_overlay

        # Page-wide "show sub pin details" toggle: when on (?children=1), the
        # map, photo gallery, and visit history all include content from this
        # pin's child pins (any depth). Off by default so the page stays
        # simple for the majority of users who never nest pins.
        include_children = request.GET.get("children") == "1"

        from urbanlens.dashboard.models.pin_list.model import PinList

        pin_lists = list(PinList.objects.filter(profile=profile).order_by("name"))

        pin_cover_candidates: list[dict] = []
        if pin.cover_photo_id:
            pin_cover_candidates = [{"id": img.pk, "url": img.image.url} for img in pin.images.exclude(pk=pin.cover_photo_id).order_by("-created")[:20] if img.image]

        return render(
            request,
            "dashboard/pages/location/index.html",
            {
                "pin": pin,
                "profile": profile,
                "parent_pin": pin.parent_pin,
                "has_child_pins": pin.detail_pins.exists(),
                "include_children": include_children,
                "can_view_debug_overlay": can_view_debug_overlay(request.user),
                "google_maps_api_key": settings.google_unrestricted_api_key,
                "openweathermap_api_key": settings.openweathermap_api_key,
                "page_name": "location-details",
                "pin_alias_suggestions": pin.aliases.order_by(Case(When(kind=AliasType.OFFICIAL, then=0), default=1), "name"),
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
                "all_categories": Label.objects.categories().ordered(),
                "default_map_view": profile.default_map_view,
                "markup_fill_color": profile.markup_fill_color,
                "markup_fill_opacity": profile.markup_fill_opacity,
                "markup_border_color": profile.markup_border_color,
                "markup_border_opacity": profile.markup_border_opacity,
                "today": today.isoformat(),
                "min_date": min_date.isoformat(),
                "security_level_choices": SecurityLevel.choices,
                "pin_lists": pin_lists,
                "pin_cover_candidates": pin_cover_candidates,
                "media_bulk_actions": [
                    {"action": "relevant", "icon": "thumb_up", "label": "Mark relevant"},
                    {"action": "not_relevant", "icon": "thumb_down", "label": "Mark not relevant"},
                    {"action": "wiki", "icon": "public", "label": "Send to wiki"},
                ],
                "detail_pin_bulk_actions": [
                    {"action": "promote", "icon": "move_up", "label": "Promote to top level"},
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

    def init_map(self, request: HttpRequest):
        map_data = self.get_map_data()

        # Preprocess data into strings
        for pin in map_data:
            if "description" in pin and pin["description"] is None:
                pin["description"] = ""

            # Turn arrays into csv
            if pin.get("tags"):
                pin["tags"] = ", ".join(pin["tags"])
            else:
                pin["tags"] = ""
            if pin.get("categories"):
                pin["categories"] = ", ".join(pin["categories"])
            else:
                pin["categories"] = ""

            # Last visited = None => Never
            if not pin["last_visited"] or pin["last_visited"] == "never":
                pin["last_visited"] = "Never"
            else:
                try:
                    # Dates look like this: 2023-01-02T00:00:00+00:00
                    pin["last_visited"] = datetime.strptime(pin["last_visited"], "%Y-%m-%dT%H:%M:%S%z").strftime(
                        "%Y-%m-%d",
                    )
                except ValueError:
                    logger.warning("Unable to parse date: %s", pin["last_visited"])

            if pin["status"]:
                pin["status"] = pin["status"].replace("_", " ").capitalize()

        return render(request, "dashboard/pages/map/data.html", {"map_data": map_data})

    def get_map_data(self):
        map_data = Pin.objects.all()
        if not map_data:
            # Default map data
            map_data = [
                {
                    "latitude": 42.65250213448323,
                    "longitude": -73.75791867436858,
                    "name": "Default Pin",
                    "description": "No pins saved yet.",
                },
            ]
        else:
            map_data = [pin.to_json() for pin in map_data]

        return map_data

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
        from urbanlens.dashboard.services.debug_overlay import DebugEntry, can_view_debug_overlay

        if not can_view_debug_overlay(request.user):
            return None
        return DebugEntry(source=source, query=query, from_cache=from_cache, count=count)

    # -- Async external-data panel plumbing --------------------------------------
    #
    # External-data panels never fetch upstream data on the request path: on a
    # store miss the controller schedules a Celery task (single-flight) and
    # returns a self-polling placeholder; polls re-enter the same endpoint with
    # ?attempt=N until the task lands the data or the attempt budget runs out.
    # See services/external_data.py for the source registry and failure policy.

    @staticmethod
    def _poll_attempt(request: HttpRequest) -> int:
        """Which poll cycle this request is (0 for the initial page-load request)."""
        try:
            return max(int(request.GET.get("attempt", "0")), 0)
        except (TypeError, ValueError):
            return 0

    def _pending_panel(self, request: HttpRequest, pin: Pin, source_key: str):
        """Schedule a panel's background fetch and return its polling placeholder.

        Args:
            request: The current request (its path doubles as the poll URL).
            pin: The pin whose panel data is being fetched.
            source_key: An ``external_data.panel_sources()`` key.

        Returns:
            The self-polling placeholder fragment, or a 204 when the source is
            suppressed or the poll budget is exhausted (the page's existing
            htmx 204 handler removes the section quietly).
        """
        from urbanlens.dashboard.services.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, get_panel_source, schedule_panel_fetch

        attempt = self._poll_attempt(request)
        if attempt >= MAX_POLL_ATTEMPTS or not schedule_panel_fetch(source_key, pin):
            return HttpResponse(status=204)
        source = get_panel_source(source_key)
        if source is None:
            return HttpResponse(status=204)
        return render(
            request,
            "dashboard/partials/pins/panel_pending.html",
            {
                "section_id": source.section_id,
                "outer_class": source.outer_class,
                "outer_is_card": source.outer_is_card,
                "icon": source.icon,
                "title": source.title,
                "poll_url": request.path,
                "next_attempt": attempt + 1,
                "poll_interval": POLL_INTERVAL_SECONDS,
            },
        )

    def _pending_media(self, request: HttpRequest, pin: Pin, source_key: str):
        """Schedule a media provider's fetch and return its polling loader.

        Media loaders differ from section panels: they're hidden divs whose
        responses append into the shared gallery grid, and the gallery JS
        counts responses to know when every provider has reported in. The
        pending response therefore (a) retargets the swap back onto the
        requesting loader itself via HX-Retarget/HX-Reswap, and (b) carries
        the UL-Panel-Pending header so the gallery JS ignores it instead of
        counting it as a provider result.

        Args:
            request: The current request (its path doubles as the poll URL).
            pin: The pin whose media is being fetched.
            source_key: One of the media ``panel_sources()`` keys.

        Returns:
            The self-polling loader fragment, or a 204 when the source is
            suppressed or the poll budget is exhausted (the gallery JS counts
            a 204 as "this provider is done, with nothing").
        """
        from urbanlens.dashboard.services.external_data import MAX_POLL_ATTEMPTS, POLL_INTERVAL_SECONDS, schedule_panel_fetch

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

        Backs the combined "Media" section on the pin detail page. Each provider
        (Smithsonian, Wikimedia Commons, Library of Congress, Yelp, Google
        Images, Google Maps, ...) is fetched via its own HTMX request targeting
        the shared gallery grid (see ``media-gallery-section`` in the pin detail
        template), so a slow provider never blocks the others from appearing.
        Every provider is a ``GalleryMediaSource``, so this view is oblivious to
        which one it's rendering.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
        from urbanlens.dashboard.services.external_data import GalleryMediaSource, get_panel_source

        panel = get_panel_source(source)
        if not isinstance(panel, GalleryMediaSource):
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

        cached = LocationCache.get_fresh(location, panel.cache_source)
        if cached is None:
            return self._pending_media(request, pin, source)
        items = panel.media_items(cached.data or {})

        profile, _ = Profile.objects.get_or_create(user=request.user)
        relevance = dict(
            MediaRelevance.objects.filter(profile=profile, location=location, source=source).values_list("item_key", "is_relevant"),
        )
        rendered_items = [{"item": item, "key": media_item_key(item.url), "is_relevant": relevance.get(media_item_key(item.url))} for item in items]

        # Render even when a provider found nothing, so admins can see what was
        # searched (including every candidate query tried) in the debug overlay
        # rather than the request silently vanishing as a 204. The template only
        # emits the hidden debug marker plus zero <a class="media-item"> tags in
        # that case, so it's a no-op for regular users and doesn't add a visible
        # empty tile to the gallery (see the media-item-count check that hides
        # the whole section when no provider found anything, in index.html).
        context = {
            "rendered_items": rendered_items,
            "source_key": source,
            "debug": self._debug_entry(request, source, cached.query_key, from_cache=True, count=len(items)),
        }
        return render(request, "dashboard/partials/pins/pin_media_items.html", context)

    @action(detail=True, methods=["post"])
    def media_relevance(self, request: Request, pin_slug: str):
        """Set (or clear) the requesting user's relevance mark on one Media gallery item."""
        from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found."}, status=404)
        if not pin.location:
            return JsonResponse({"error": "Pin has no location."}, status=400)

        try:
            data = request.data
            source = str(data["source"])[:30]
            url = str(data["url"])
            is_relevant = data.get("is_relevant")
        except (KeyError, ValueError, TypeError, ParseError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        item_key = data.get("item_key") or media_item_key(url)
        profile, _ = Profile.objects.get_or_create(user=request.user)

        if is_relevant is None:
            MediaRelevance.objects.filter(profile=profile, location=pin.location, source=source, item_key=item_key).delete()
            return JsonResponse({"is_relevant": None})

        MediaRelevance.objects.update_or_create(
            profile=profile,
            location=pin.location,
            source=source,
            item_key=item_key,
            defaults={"is_relevant": bool(is_relevant)},
        )
        return JsonResponse({"is_relevant": bool(is_relevant)})

    @action(detail=True, methods=["post"])
    def media_send_to_wiki(self, request: Request, pin_slug: str):
        """Materialize selected Media gallery items and attach them to this location's wiki."""
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.media_materialize import MaterializeError, materialize_media_item

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
            data = request.data
            items = data["items"]
        except (KeyError, TypeError, ParseError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        created = 0
        errors: list[str] = []
        for entry in items[:20]:
            try:
                materialize_media_item(
                    location=pin.location,
                    profile=profile,
                    source=str(entry.get("source", ""))[:30],
                    url=str(entry["url"]),
                    page_url=str(entry.get("page_url") or ""),
                    caption=str(entry.get("caption") or ""),
                    wiki=wiki,
                )
                created += 1
            except MaterializeError as exc:
                logger.warning("media_send_to_wiki: failed to materialize %s: %s", entry.get("url"), exc)
                errors.append(str(exc))
            except (KeyError, TypeError, ValueError):
                logger.warning("media_send_to_wiki: malformed item entry: %r", entry)

        return JsonResponse({"created": created, "errors": errors})

    @action(detail=True, methods=["get"])
    def nearby_pins_json(self, request: Request, pin_slug: str):
        """Return the profile's other pins near this one, for the "Nearby Pins" map layer.

        Off by default on the pin detail page map - only fetched once the
        user turns the layer on.
        """
        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "Pin not found."}, status=404)
        if not pin.location:
            return JsonResponse({"pins": []})

        nearby = Pin.objects.filter(profile=pin.profile).exclude(pk=pin.pk).near_point(pin.location.point, radius_km=5).select_related("location")[:200]
        return JsonResponse({"pins": [p.to_detail_json() for p in nearby]})

    @action(detail=False, methods=["post"])
    def set_media_sort(self, request: Request):
        """Persist the requesting user's Media gallery sort-order preference."""
        try:
            data = request.data
            sort = data.get("sort")
        except ParseError:
            return JsonResponse({"error": "Invalid request data."}, status=400)
        if sort not in ("relevant", "recent"):
            return JsonResponse({"error": "Invalid sort value."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        profile.media_gallery_sort = sort
        profile.save(update_fields=["media_gallery_sort", "updated"])
        return JsonResponse({"sort": sort})

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
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin: Pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Only search when we have an official identifier for the place -- a
        # personal pin label alone produces noisy, irrelevant search results.
        if not pin.meaningful_official_name:
            return HttpResponse("", status=204)

        search_name = pin.get_unique_search_name(quote_name=True)
        if not search_name:
            return HttpResponse("", status=204)

        if not user_has_feature(request.user, SiteFeature.SEARCH):
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {"pin": pin, "error": "Web search is available to VIP subscribers."},
                status=403,
            )

        location = pin.location
        # Shared across every pin/wiki at this Location, keyed on the search
        # query text -- two pins with the same effective name (the common
        # case) hit the same cache entry instead of each paying for their own.
        # A pin with a custom override name produces a different query, which
        # is treated as a miss rather than serving another pin's results.
        cached = LocationCache.get_fresh(location, "web_search") if location else None
        if cached is not None and cached.query_key != search_name:
            cached = None

        can_refresh = cached is not None and timezone.now() - cached.updated >= _WEB_SEARCH_MIN_REFRESH_AGE

        if force_refresh:
            if not can_refresh:
                return HttpResponse("Search results were cached too recently to refresh.", status=429)
            cached = None

        if cached is not None:
            results = cached.data.get("results", [])
            page_obj = get_page(request, results, _WEB_SEARCH_PAGE_SIZE)
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {
                    "pin": pin,
                    "search_results": page_obj.object_list,
                    "page_obj": page_obj,
                    "adaptive_pagination": True,
                    "can_refresh": can_refresh,
                    "debug": self._debug_entry(request, "web_search", search_name, from_cache=True, count=len(results)),
                },
            )

        from urbanlens.dashboard.services.timeout_utils import EXTERNAL_CALL_DEADLINE, call_with_deadline

        try:
            search_gateway = get_search_gateway()
            # Deadline-bounded: this is the one external fetch still made on
            # the request path (interactive, VIP-gated, and cached below), so
            # a slow search backend degrades to the error card instead of
            # holding the request open.
            search_results = call_with_deadline(
                lambda: search_gateway.search(search_name),
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
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Unable to contact web search API: %s", e)
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {"pin": pin, "error": "Search unavailable. Please try again later."},
            )

        for r in search_results:
            try:
                r["domain"] = urlparse(r.get("link", "")).netloc.removeprefix("www.")
            except (ValueError, AttributeError):
                r["domain"] = ""
            r["date_display"] = format_search_date(r.get("date"))

        if location:
            LocationCache.set(location, "web_search", {"results": search_results}, query_key=search_name)

        page_obj = get_page(request, search_results, _WEB_SEARCH_PAGE_SIZE)
        return render(
            request,
            "dashboard/pages/location/web_search.html",
            {
                "pin": pin,
                "search_results": page_obj.object_list,
                "page_obj": page_obj,
                "adaptive_pagination": True,
                "can_refresh": False,
                "debug": self._debug_entry(request, "web_search", search_name, from_cache=False, count=len(search_results)),
            },
        )

    def satellite_view_carousell(self, request: HttpRequest, **kwargs):
        """Returns an HTML fragment with a multi-source satellite imagery carousel.

        Sources included (where available):
        - Google Maps Static API (current, high-res) - fetched server-side
        - Esri World Imagery Export (current, high-res) - URL-based
        - USGS National Map Imagery (current, US only) - URL-based
        - Esri Wayback historical releases - URL-based export
        - NASA GIBS / Landsat Annual (2011-2019) - WMS URL-based
        - Mapbox Satellite (current, high-res) - fetched server-side
        - Bing Maps Aerial (current, high-res) - fetched server-side
        - OpenAerialMap community imagery - browser-loaded thumbnails
        """
        from urbanlens.dashboard.services.external_data import collect_satellite_slides, panel_sources
        from urbanlens.dashboard.services.timeout_utils import EXTERNAL_CALL_DEADLINE, call_with_deadline

        try:
            pin = Pin.objects.select_related("location").get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            return render(
                request,
                "dashboard/pages/location/satellite_view.html",
                {"error": "No coordinates available."},
            )

        # First visit for these coordinates: warm every provider's slide cache
        # in a Celery task and let the placeholder poll -- the provider chain
        # is six sequential upstreams and must never run on the request path.
        if not panel_sources()["satellite"].is_ready(pin):
            return self._pending_panel(request, pin, "satellite")

        # Ready: the same collector now runs against warm per-provider caches,
        # so this is normally instant. The deadline guards the rare gap where
        # an individual provider's entry was evicted before the ready marker
        # expired -- bounded staleness beats an unbounded inline refetch.
        coord_query = f"{lat:.5f}, {lng:.5f}"
        satellite_default: tuple[list[SatelliteSlide], list[ProviderFetchResult]] = ([], [])
        slides, provider_results = call_with_deadline(
            lambda: collect_satellite_slides(float(lat), float(lng)),
            timeout=EXTERNAL_CALL_DEADLINE,
            default=satellite_default,
            name="satellite-replay",
        )
        # Failures surface as count=0 entries, matching the old inline behaviour.
        debug_entries = []
        for result in provider_results:
            if entry := self._debug_entry(request, result.service, coord_query, from_cache=result.from_cache, count=result.count):
                debug_entries.append(entry)

        return render(
            request,
            "dashboard/pages/location/satellite_view.html",
            {"slides": slides, "lat": lat, "lng": lng, "pin": pin, "debug_entries": debug_entries},
        )

    def street_view(self, request: HttpRequest, **kwargs):
        """Returns an HTML fragment with a multi-source street-view carousel.

        Sources included (where available):
        - Google Street View (fetched server-side, cached 30 days)
        - Mapillary crowdsourced imagery (browser-loaded URLs, cached 24 h)
        - KartaView open imagery (browser-loaded URLs, cached 24 h)
        """
        from urbanlens.dashboard.services.external_data import collect_street_view_slides, panel_sources
        from urbanlens.dashboard.services.timeout_utils import EXTERNAL_CALL_DEADLINE, call_with_deadline

        try:
            pin = Pin.objects.select_related("location").get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            return render(request, "dashboard/pages/location/street_view.html", {"error": "No coordinates available."})

        # See satellite_view_carousell: warm provider caches in Celery on first
        # visit, then render from those caches with a deadline safety net.
        if not panel_sources()["street_view"].is_ready(pin):
            return self._pending_panel(request, pin, "street_view")

        coord_query = f"{lat:.5f}, {lng:.5f}"
        street_view_default: tuple[list[StreetViewSlide], list[ProviderFetchResult]] = ([], [])
        slides, provider_results = call_with_deadline(
            lambda: collect_street_view_slides(float(lat), float(lng)),
            timeout=EXTERNAL_CALL_DEADLINE,
            default=street_view_default,
            name="street-view-replay",
        )
        debug_entries = []
        for result in provider_results:
            if entry := self._debug_entry(request, result.service, coord_query, from_cache=result.from_cache, count=result.count):
                debug_entries.append(entry)

        return render(
            request,
            "dashboard/pages/location/street_view.html",
            {
                "slides": slides,
                "pin": pin,
                "google_maps_api_key": settings.google_public_api_key,
                "debug_entries": debug_entries,
            },
        )

    @action(detail=True, methods=["get"])
    def import_form(self, request: HttpRequest):
        """View the import wizard dialog.

        The same wizard powers both the pin importer and the Memories
        "Import routes & history" flow; only the surrounding copy differs.

        Args:
            request: The incoming request. An optional ``variant`` query
                parameter of ``"memories"`` swaps the dialog's title and intro
                text for routes/location-history wording; any other value uses
                the default pin-import wording.

        Returns:
            The rendered import wizard dialog template.
        """
        from urbanlens.dashboard.models.labels.model import Label

        profile = Profile.objects.get(user=request.user)
        tags = Label.objects.visible_to(profile).ordered()
        variant = "memories" if request.GET.get("variant") == "memories" else "pins"
        import_title = "Import Routes & History" if variant == "memories" else "Import Pins"
        return render(
            request,
            "dashboard/pages/location/import/csv.html",
            {
                "form": UploadDataFile(),
                "tags": tags,
                "profile": profile,
                "import_variant": variant,
                "import_title": import_title,
                "import_review_title": "Review Import",
                # can_upload_videos/can_use_ai_features come from the
                # add_feature_access context processor (see settings/base.py),
                # not set explicitly here.
            },
        )

    @action(detail=True, methods=["post"])
    def upload_takeout(self, request: HttpRequest):
        """
        Upload one or more Google Takeout files and stream import progress as SSE.

        Accepts individual KML, JSON, and CSV files as well as ZIP and TGZ archives.
        Archives are extracted securely before parsing; malformed or unsupported
        entries are skipped without aborting the whole import.
        """
        from urbanlens.dashboard.services.archive_extractor import extract_archive, is_archive

        form = UploadDataFile(request.POST, request.FILES)
        if not form.is_valid():
            return JsonResponse({"error": "Invalid form"}, status=400)

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)

        uploaded_files = form.cleaned_data["upload_files"]

        # Expand every uploaded file into a flat list of (name, raw_bytes) pairs,
        # recursing one level to handle KMZ (ZIP-inside-ZIP) found in an archive.
        all_files: list[tuple[str, bytes]] = []
        for uploaded_file in uploaded_files:
            try:
                data = uploaded_file.read()
            except OSError as e:
                logger.exception("Failed to read uploaded file %s -> %s", uploaded_file.name, e)
                return JsonResponse(
                    {"error": f"Failed to read {uploaded_file.name}."},
                    status=400,
                )

            if is_archive(data):
                try:
                    extracted = extract_archive(data)
                except ValueError as exc:
                    logger.warning("Could not extract archive: %s", exc)
                    return JsonResponse({"error": "Invalid archive."}, status=400)

                for entry in extracted:
                    # Handle KMZ files (nested ZIPs) found inside an outer archive.
                    if is_archive(entry.data):
                        try:
                            inner = extract_archive(entry.data)
                            all_files.extend((x.name, x.data) for x in inner)
                        except ValueError:
                            logger.warning("Could not extract nested archive: %s", entry.name)
                    else:
                        all_files.append((entry.name, entry.data))
            else:
                all_files.append((uploaded_file.name, data))

        profile, _ = Profile.objects.get_or_create(user=request.user)

        from urbanlens.dashboard.models.labels.model import Label

        tag_ids = request.POST.getlist("tag_ids")
        import_tags = list(Label.objects.visible_to(profile).filter(id__in=tag_ids)) if tag_ids else []
        tag_by_filename = request.POST.get("tag_by_filename") == "1"

        google_maps_gateway = GoogleMapsGateway()

        response = StreamingHttpResponse(
            google_maps_gateway.import_pins_streaming(
                all_files,
                profile,
                tags=import_tags,
                tag_by_filename=tag_by_filename,
            ),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    @action(detail=False, methods=["post"])
    def parse_for_preview(self, request: HttpRequest):
        """Parse uploaded files and return pin preview data as JSON without importing."""
        import json as _json

        from urbanlens.dashboard.models.labels.model import Label
        from urbanlens.dashboard.services.archive_extractor import extract_archive, is_archive

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)

        form = UploadDataFile(request.POST, request.FILES)
        if not form.is_valid():
            return JsonResponse({"error": "Invalid form."}, status=400)

        from urbanlens.dashboard.services.ai.document_import import (
            DocumentTooLargeError,
            extract_pins_from_document,
            is_supported_document_filename,
        )

        uploaded_files = form.cleaned_data["upload_files"]

        all_files: list[tuple[str, bytes]] = []
        document_files: list[tuple[str, bytes]] = []
        for uploaded_file in uploaded_files:
            try:
                data = uploaded_file.read()
            except OSError as exc:
                logger.warning("Failed to read uploaded file %s -> %s", uploaded_file.name, exc)
                return JsonResponse({"error": f"Failed to read {uploaded_file.name}."}, status=400)

            # .txt/.docx are routed to the AI extraction pipeline below rather than the
            # geo-format dispatch - a .docx in particular starts with ZIP magic bytes and
            # would otherwise be misidentified as a location-data archive.
            if is_supported_document_filename(uploaded_file.name or ""):
                document_files.append((uploaded_file.name, data))
            elif is_archive(data):
                try:
                    extracted = extract_archive(data)
                except ValueError as exc:
                    logger.warning("Could not extract archive: %s", exc)
                    return JsonResponse({"error": "Invalid archive."}, status=400)
                for entry in extracted:
                    if is_archive(entry.data):
                        try:
                            inner = extract_archive(entry.data)
                            all_files.extend((x.name, x.data) for x in inner)
                        except ValueError:
                            logger.warning("Could not extract nested archive during preview")
                    else:
                        all_files.append((entry.name, entry.data))
            else:
                all_files.append((uploaded_file.name, data))

        profile, _ = Profile.objects.get_or_create(user=request.user)
        gateway = GoogleMapsGateway()

        lists = gateway.parse_for_preview(all_files, profile)

        document_warnings: list[str] = []
        for doc_name, doc_data in document_files:
            try:
                doc_list, doc_warning = extract_pins_from_document(doc_name, doc_data, profile)
            except DocumentTooLargeError:
                document_warnings.append(f"Document too large: {doc_name}")
                continue
            if doc_warning:
                document_warnings.append(doc_warning)
            if doc_list:
                lists.append(doc_list)

        if not lists:
            return JsonResponse(
                {"error": document_warnings[0] if document_warnings else "No valid location files found in the upload."},
                status=400,
            )

        labels = Label.objects.visible_to(profile).ordered()

        return JsonResponse(
            {
                "lists": lists,
                "total": sum(len(lst["pins"]) for lst in lists),
                "labels": [
                    {
                        "id": b.id,
                        "name": b.name,
                        "color": b.color or "",
                        "icon": b.icon or "",
                        "kind": b.kind,
                    }
                    for b in labels
                ],
                "warnings": document_warnings,
            },
        )

    # -- External-data HTMX endpoints -------------------------------------------

    def wikipedia_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Wikipedia article summary for the pin's location.

        Returns an empty 204 when no matching article is found; the client-side
        htmx:afterOnLoad handler removes the loading placeholder on 204.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            logger.debug("wikipedia_info: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        if not lat and not lng:
            logger.debug("wikipedia_info: pin %s has no coordinates, skipping", pin_slug)
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "wikipedia")
        if cached is None:
            return self._pending_panel(request, pin, "wikipedia")
        data = cached.data or None

        if not data:
            logger.debug("wikipedia_info: no article found for pin %s at (%s, %s)", pin_slug, lat, lng)
            return HttpResponse(status=204)

        context = {"article": data, "debug": self._debug_entry(request, "wikipedia", cached.query_key, from_cache=True, count=1)}
        return render(request, "dashboard/partials/pins/pin_wikipedia.html", context)

    def loopnet_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: LoopNet commercial real-estate data for the pin's address.

        Requires a full street address; returns 204 when none is available or
        when the search/scrape produces no results.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.loopnet import LoopnetPanelSource

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            logger.debug("loopnet_info: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        # Requires at least street + city precision to search against.
        address = LoopnetPanelSource.address(pin)
        if not address:
            logger.debug("loopnet_info: pin %s has insufficient address data (route=%r), skipping", pin_slug, location.route)
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "loopnet")
        if cached is None:
            return self._pending_panel(request, pin, "loopnet")
        data = cached.data or None

        if not data or not data.get("listings"):
            logger.debug("loopnet_info: no listings found for pin %s (address=%r)", pin_slug, address)
            return HttpResponse(status=204)

        context = {
            "result": data,
            "address": address,
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
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.plugins.builtin.yelp import YelpPanelSource

        panel = YelpPanelSource()

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)
        if not panel.gate(pin):
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, panel.cache_source)
        if cached is None:
            return self._pending_panel(request, pin, "yelp")
        data = cached.data or None
        business = (data or {}).get("business")
        if not business:
            return HttpResponse(status=204)

        reviews = (data or {}).get("reviews") or []
        context = {
            "business": business,
            "latest_review": reviews[0] if reviews else None,
            "debug": self._debug_entry(request, "yelp", cached.query_key, from_cache=True, count=1),
        }
        return render(request, "dashboard/partials/pins/pin_yelp.html", context)

    def nps_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: National Park Service information for the pin's location.

        Shows the NPS unit whose boundary *contains* the pin's coordinates, if
        any -- the panel is about the pinned place being inside a national park,
        not merely near one. Requires an NPS API key.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if not settings.nps_api_key:
            logger.debug("nps_info: NPS API key not configured, skipping pin %s", pin_slug)
            return HttpResponse(status=204)

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            logger.debug("nps_info: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            logger.debug("nps_info: pin %s missing lat/lng, skipping", pin_slug)
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "nps")
        if cached is None:
            return self._pending_panel(request, pin, "nps")
        data = cached.data or None

        if not data:
            logger.debug("nps_info: pin %s is not within any NPS unit", pin_slug)
            return HttpResponse(status=204)

        context = {"park": data, "debug": self._debug_entry(request, "nps", cached.query_key, from_cache=True, count=1)}
        return render(request, "dashboard/partials/pins/pin_nps.html", context)

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
            return self._pending_panel(request, pin, "nominatim")
        data = cached.data or None

        useful_fields = ("website", "phone", "opening_hours", "operator", "wikipedia", "wikidata", "image", "extra_details", "kind_label")
        if not data or not any(data.get(k) for k in useful_fields):
            logger.debug("nominatim_info: no enrichment data for pin %s at (%s, %s)", pin_slug, redact_coordinate(lat), redact_coordinate(lng))
            return HttpResponse(status=204)

        context = {"place": data, "debug": self._debug_entry(request, "nominatim", cached.query_key, from_cache=True, count=1)}
        return render(request, "dashboard/partials/pins/pin_nominatim.html", context)

    def azure_maps_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Azure Maps reverse-geocoded address and nearest-POI details for the pin's location.

        Only renders when the payload carries a formatted address or a nearby POI - a
        coordinate-only result (nothing geocoded, nothing nearby) returns 204.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if not settings.azure_maps_subscription_key:
            logger.debug("azure_maps_info: Azure Maps subscription key not configured, skipping pin %s", pin_slug)
            return HttpResponse(status=204)

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            logger.debug("azure_maps_info: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            logger.debug("azure_maps_info: pin %s has no coordinates, skipping", pin_slug)
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "azure_maps")
        if cached is None:
            return self._pending_panel(request, pin, "azure_maps")
        data = cached.data or None

        if not data or not (data.get("formatted_address") or data.get("poi")):
            logger.debug("azure_maps_info: no enrichment data for pin %s at (%s, %s)", pin_slug, redact_coordinate(lat), redact_coordinate(lng))
            return HttpResponse(status=204)

        context = {"place": data, "debug": self._debug_entry(request, "azure_maps", cached.query_key, from_cache=True, count=1)}
        return render(request, "dashboard/partials/pins/pin_azure_maps.html", context)

    def photon_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Photon (Komoot) reverse-geocoded address for the pin's location.

        Free, keyless, OSM-backed geocoder shown as a redundant cross-check alongside
        the Nominatim panel. Returns 204 when nothing resolved.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "photon")
        if cached is None:
            return self._pending_panel(request, pin, "photon")
        data = cached.data or None

        if not data or not data.get("name"):
            return HttpResponse(status=204)

        chips = [data["osm_value"].replace("_", " ").title()] if data.get("osm_value") else []
        street_parts = [data[key] for key in ("housenumber", "street") if data.get(key)]
        meta = [{"label": "Street", "value": " ".join(street_parts)}] if street_parts else []
        for key, label in (("locality", "Locality"), ("district", "District"), ("city", "City"), ("county", "County"), ("state", "State"), ("country", "Country"), ("postcode", "Postal Code")):
            if data.get(key):
                meta.append({"label": label, "value": data[key]})

        context = {
            "section_id": "photon-section",
            "icon": "person_pin_circle",
            "title": "Photon (OpenStreetMap)",
            "heading_name": data.get("name"),
            "chips": chips,
            "meta": meta,
            "debug": self._debug_entry(request, "photon", cached.query_key, from_cache=True, count=1),
        }
        return render(request, "dashboard/partials/pins/_simple_info_panel.html", context)

    def census_tigerweb_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: US Census geography (state/county/place/tract) for the pin's location.

        Free, keyless US Census Bureau lookup. Returns 204 outside the US (no state
        geography resolves) or when the location has no coordinates.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "census_tigerweb")
        if cached is None:
            return self._pending_panel(request, pin, "census_tigerweb")
        data = cached.data or None

        data = data or {}
        state = data.get("state")
        if not state:
            return HttpResponse(status=204)

        meta = [{"label": "State", "value": state["name"]}]
        for key, label in (("county", "County"), ("place", "Place"), ("tract", "Census Tract")):
            entry = data.get(key)
            if entry and entry.get("name"):
                meta.append({"label": label, "value": entry["name"]})

        context = {
            "section_id": "census-tigerweb-section",
            "icon": "flag",
            "title": "US Census Geography",
            "meta": meta,
            "debug": self._debug_entry(request, "census_tigerweb", cached.query_key, from_cache=True, count=1),
        }
        return render(request, "dashboard/partials/pins/_simple_info_panel.html", context)

    def epa_echo_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: nearby EPA-regulated facilities and their compliance status.

        Free, keyless EPA ECHO lookup, USA only. Returns 204 outside the US or when
        no regulated facilities are nearby.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "epa_echo")
        if cached is None:
            return self._pending_panel(request, pin, "epa_echo")
        data = cached.data or None

        facilities = (data or {}).get("facilities") or []
        if not facilities:
            return HttpResponse(status=204)

        meta = []
        for facility in facilities[:8]:
            status = facility.get("compliance_status") or "Unknown"
            if facility.get("significant_violator"):
                status = f"{status} (significant violator)"
            meta.append({"label": facility.get("name") or "Unnamed facility", "value": f"{facility.get('address') or ''} - {status}".strip(" -")})

        context = {
            "section_id": "epa-echo-section",
            "icon": "factory",
            "title": "EPA Regulated Facilities",
            "chips": [f"{len(facilities)} nearby"],
            "meta": meta,
            "footer_link": {"url": "https://echo.epa.gov/", "label": "View on EPA ECHO"},
            "debug": self._debug_entry(request, "epa_echo", cached.query_key, from_cache=True, count=len(facilities)),
        }
        return render(request, "dashboard/partials/pins/_simple_info_panel.html", context)

    def inaturalist_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: recent iNaturalist observations near the pin's location.

        Free, keyless. Returns 204 when nothing has been recorded nearby.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "inaturalist")
        if cached is None:
            return self._pending_panel(request, pin, "inaturalist")
        data = cached.data or None

        observations = (data or {}).get("observations") or []
        if not observations:
            return HttpResponse(status=204)

        meta = [
            {"label": obs.get("common_name") or obs.get("scientific_name") or "Unknown species", "value": obs.get("observed_on") or "Date unknown"}
            for obs in observations[:8]
        ]

        context = {
            "section_id": "inaturalist-section",
            "icon": "forest",
            "title": "iNaturalist",
            "chips": [f"{len(observations)} nearby"],
            "meta": meta,
            "footer_link": {"url": "https://www.inaturalist.org/observations", "label": "View on iNaturalist"},
            "debug": self._debug_entry(request, "inaturalist", cached.query_key, from_cache=True, count=len(observations)),
        }
        return render(request, "dashboard/partials/pins/_simple_info_panel.html", context)

    def gdelt_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: recent news coverage of the pin's location, via GDELT.

        Free, keyless. Returns 204 when the pin has no usable search name or
        nothing was found.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.location:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(pin.location, "gdelt")
        if cached is None:
            return self._pending_panel(request, pin, "gdelt")
        data = cached.data or None

        articles = (data or {}).get("articles") or []
        if not articles:
            return HttpResponse(status=204)

        meta = [
            {"label": article.get("date") or "Undated", "value": article.get("title") or article.get("domain") or "", "href": article.get("url") or ""}
            for article in articles[:8]
        ]

        context = {
            "section_id": "gdelt-section",
            "icon": "newspaper",
            "title": "News",
            "meta": meta,
            "debug": self._debug_entry(request, "gdelt", cached.query_key, from_cache=True, count=len(articles)),
        }
        return render(request, "dashboard/partials/pins/_simple_info_panel.html", context)

    def overture_building_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Overture Maps building characteristics for the pin's location.

        Free, open-data. Returns 204 when no Overture building footprint
        contains the pin's coordinates.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.location:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(pin.location, "overture_building_attributes")
        if cached is None:
            return self._pending_panel(request, pin, "overture_building_attributes")
        data = cached.data or None

        if not data:
            return HttpResponse(status=204)

        chips = [data["subtype"].replace("_", " ").title()] if data.get("subtype") else []
        meta = []
        if data.get("height_m"):
            meta.append({"label": "Height", "value": f"{data['height_m']:.0f} m"})
        if data.get("num_floors"):
            meta.append({"label": "Floors", "value": str(data["num_floors"])})
        if data.get("roof_shape"):
            meta.append({"label": "Roof Shape", "value": data["roof_shape"].replace("_", " ").title()})
        if data.get("roof_material"):
            meta.append({"label": "Roof Material", "value": data["roof_material"].replace("_", " ").title()})

        if not chips and not meta:
            return HttpResponse(status=204)

        context = {
            "section_id": "overture-building-section",
            "icon": "apartment",
            "title": "Building Characteristics",
            "heading_name": data.get("primary_name"),
            "chips": chips,
            "meta": meta,
            "debug": self._debug_entry(request, "overture_building_attributes", cached.query_key, from_cache=True, count=1),
        }
        return render(request, "dashboard/partials/pins/_simple_info_panel.html", context)

    def usgs_earthquakes_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: recent nearby seismic activity, via USGS.

        Free, keyless. Returns 204 when nothing of note has occurred nearby.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "usgs_earthquakes")
        if cached is None:
            return self._pending_panel(request, pin, "usgs_earthquakes")
        data = cached.data or None

        events = (data or {}).get("events") or []
        if not events:
            return HttpResponse(status=204)

        meta = []
        for event in events[:8]:
            occurred_at = event.get("occurred_at") or ""
            date_label = occurred_at[:10] if occurred_at else "Unknown date"
            magnitude = event.get("magnitude")
            meta.append(
                {
                    "label": f"M{magnitude:.1f}" if isinstance(magnitude, (int, float)) else "Unknown magnitude",
                    "value": f"{event.get('place') or 'Unknown location'} - {date_label}",
                    "href": event.get("url") or "",
                },
            )

        context = {
            "section_id": "usgs-earthquakes-section",
            "icon": "vibration",
            "title": "Recent Seismic Activity",
            "chips": [f"{len(events)} in the last 10 years"],
            "meta": meta,
            "debug": self._debug_entry(request, "usgs_earthquakes", cached.query_key, from_cache=True, count=len(events)),
        }
        return render(request, "dashboard/partials/pins/_simple_info_panel.html", context)

    def usgs_topo_info(self, request: HttpRequest, pin_slug: str):
        """HTMX partial: USGS Historical Topographic Map Collection maps near the pin.

        Queries the USGS TNMAccess public API for HTMC products (scanned historical
        topo maps going back to the late 1800s).  No API key is required.  Returns
        204 for non-US locations or when no maps are found within the search area.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if not lat or not lng:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "usgs_topo")
        if cached is None:
            return self._pending_panel(request, pin, "usgs_topo")

        maps_list = (cached.data or {}).get("items") or []
        if not maps_list:
            logger.debug("usgs_topo_info: no topo maps found for pin %s", pin_slug)
            return HttpResponse(status=204)

        context = {
            "maps": maps_list[:20],
            "debug": self._debug_entry(request, "usgs_topo", cached.query_key, from_cache=True, count=len(maps_list)),
        }
        return render(request, "dashboard/partials/pins/pin_usgs_topo.html", context)

    # Sources rendered via LocationCache on the pin detail page (see the endpoints above).
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
        from urbanlens.dashboard.services.debug_overlay import can_view_debug_overlay

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
        """Stream SSE import progress for user-confirmed pin selections from the preview step."""
        import json as _json

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)

        try:
            payload = request.data
            confirmed_lists = payload.get("lists", [])
            auto_tag = bool(payload.get("auto_tag", True))
        except (ValueError, KeyError):
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)

        if not confirmed_lists:
            return JsonResponse({"error": "No lists provided."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        gateway = GoogleMapsGateway()

        response = StreamingHttpResponse(
            gateway.import_preview_streaming(confirmed_lists, profile, auto_tag=auto_tag),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def weather_forecast(self, request: HttpRequest, pin_slug):
        """
        Returns the weather forecast for a pin.

        Tries OpenWeatherMap first when a key is configured; falls back to the
        free, keyless Open-Meteo gateway when it isn't configured or its call
        fails, so the widget still works out of the box. Both providers
        render through the same normalized ``ForecastSlot`` shape.
        """
        from urbanlens.dashboard.services.apis.weather.forecast import owm_item_to_slot

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.external_apis_enabled:
            return HttpResponse("External weather lookups are turned off in your settings.", status=403)

        # Get the pin
        try:
            pin: Pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        if not pin.location.latitude or not pin.location.longitude:
            return HttpResponse("Pin does not have valid coordinates", status=400)

        forecast = None
        if settings.openweathermap_api_key:
            from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway

            try:
                raw_forecast = OpenWeatherMapGateway().get_weather_forecast(pin.location.latitude, pin.location.longitude)
            except Exception:
                logger.warning("OpenWeatherMap forecast failed for pin %s, falling back to Open-Meteo", pin_slug, exc_info=True)
                raw_forecast = None
            if raw_forecast:
                forecast = [slot for item in raw_forecast if (slot := owm_item_to_slot(item)) is not None]

        if not forecast:
            from urbanlens.dashboard.services.apis.weather.open_meteo import OpenMeteoGateway

            forecast = OpenMeteoGateway().get_weather_forecast(float(pin.location.latitude), float(pin.location.longitude))

        logger.debug("forecast_data: %s", forecast)

        return render(request, "dashboard/pages/location/weather.html", {"forecast": forecast})
