from __future__ import annotations

import base64
from datetime import datetime
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from rest_framework.decorators import action
from rest_framework.viewsets import GenericViewSet

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.forms.upload_datafile import UploadDataFile
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
from urbanlens.dashboard.services.apis.assets.smithsonian import SmithsonianGateway
from urbanlens.dashboard.services.apis.locations.bing_maps import BingMapsGateway
from urbanlens.dashboard.services.apis.locations.esri import EsriGateway
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.apis.locations.kartaview import KartaViewGateway
from urbanlens.dashboard.services.apis.locations.mapbox import MapboxGateway
from urbanlens.dashboard.services.apis.locations.mapillary import MapillaryGateway
from urbanlens.dashboard.services.apis.locations.nasa_gibs import NasaGibsGateway
from urbanlens.dashboard.services.apis.locations.open_aerial_map import OpenAerialMapGateway
from urbanlens.dashboard.services.apis.locations.usgs import UsgsGateway
from urbanlens.dashboard.services.pagination import get_page
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError, RequestCancelledError
from urbanlens.dashboard.services.search import format_search_date, get_search_gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.services.apis.locations.base import SatelliteSlide, SatelliteViewProvider, StreetViewProvider, StreetViewSlide

logger = logging.getLogger(__name__)

_WIKIMEDIA_CLIENT_PAGE_SIZE = 12
_WEB_SEARCH_CLIENT_PAGE_SIZE = 5
_SMITHSONIAN_CLIENT_PAGE_SIZE = 12
_ADAPTIVE_PAGE_BATCH_MULTIPLIER = 2
_WIKIMEDIA_PAGE_SIZE = _WIKIMEDIA_CLIENT_PAGE_SIZE * _ADAPTIVE_PAGE_BATCH_MULTIPLIER
_WEB_SEARCH_PAGE_SIZE = _WEB_SEARCH_CLIENT_PAGE_SIZE * _ADAPTIVE_PAGE_BATCH_MULTIPLIER
_SMITHSONIAN_PAGE_SIZE = _SMITHSONIAN_CLIENT_PAGE_SIZE * _ADAPTIVE_PAGE_BATCH_MULTIPLIER


class PinController(LoginRequiredMixin, GenericViewSet):
    """
    Controller for the pin page
    """

    def view(self, request: HttpRequest, **kwargs):
        """
        View the pin page
        """
        from datetime import date

        from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, Badge
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import PinType

        try:
            pin = Pin.objects.select_related("location").get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            try:
                pin = Pin.objects.select_related("location").get(uuid=kwargs["pin_slug"], profile__user=request.user)
            except (Pin.DoesNotExist, ValueError):
                return render(
                    request,
                    "dashboard/pages/errors/pin_not_found.html",
                    {"pin_slug": kwargs.get("pin_slug")},
                    status=404,
                )

        # Auto-link legacy pins that pre-date the Location requirement.
        # Private pins are intentionally unlinked - never create a wiki entry for them.
        if not pin.is_private and pin.location is None and pin.effective_latitude and pin.effective_longitude:
            lat, lon = pin.effective_latitude, pin.effective_longitude
            location = Location.objects.get_for_point(lat, lon)
            if not location:
                from urbanlens.dashboard.controllers.maps import _create_location_with_canonical_name

                location = _create_location_with_canonical_name(lat, lon)
            pin.location = location
            pin.save(update_fields=["location"])
        elif pin.location and not pin.location.slug:
            pin.location.ensure_slug()

        # Backfill slug for legacy pins created before slug generation was automatic.
        if not pin.slug:
            pin.slug = pin.ensure_slug()
            pin.save(update_fields=["slug"])

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

        return render(
            request,
            "dashboard/pages/location/index.html",
            {
                "pin": pin,
                "google_maps_api_key": settings.google_unrestricted_api_key,
                "openweathermap_api_key": settings.openweathermap_api_key,
                "page_name": "location-details",
                "pin_type_choices": PinType.choices,
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
                "all_categories": Badge.objects.categories().ordered(),
                "default_map_view": profile.default_map_view,
                "markup_fill_color": profile.markup_fill_color,
                "markup_fill_opacity": profile.markup_fill_opacity,
                "markup_border_color": profile.markup_border_color,
                "markup_border_opacity": profile.markup_border_opacity,
                "today": today.isoformat(),
                "min_date": min_date.isoformat(),
                "security_level_choices": SecurityLevel.choices,
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

    def get_smithsonian_images(self, request: HttpRequest, pin_slug):
        """
        Returns the Smithsonian images for a pin.
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(slug=pin_slug)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        search_name = pin.get_unique_search_name()
        if not search_name:
            return HttpResponse(status=204)

        # Instantiate the SmithsonianGateway with the API key
        smithsonian_gateway = SmithsonianGateway(api_key=settings.smithsonian_api_key or "")

        # Get historic images from the Smithsonian's API; discard entries without a usable URL
        smithsonian_images = [img for img in smithsonian_gateway.get_data(search_name) if img.get("url")]
        page_obj = get_page(request, smithsonian_images, _SMITHSONIAN_PAGE_SIZE)

        return render(
            request,
            "dashboard/pages/location/smithsonian.html",
            {
                "images": page_obj.object_list,
                "page_obj": page_obj,
                "adaptive_pagination": True,
            },
        )

    def web_search(self, request: HttpRequest, pin_slug):
        """
        Returns the web search results for a pin.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings

        try:
            pin: Pin = Pin.objects.get(slug=pin_slug)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        search_name = pin.get_unique_search_name()
        if not search_name:
            return HttpResponse("", status=204)

        if not user_has_feature(request.user, SiteFeature.SEARCH):
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {"error": "Web search is available to VIP subscribers."},
                status=403,
            )

        cache_key = make_cache_key("web_search_pin", str(pin.pk))
        site_settings = SiteSettings.get_current()
        cache_hours = site_settings.search_cache_hours

        if cache_hours > 0:
            cached = cache.get(cache_key)
            if cached is not None:
                page_obj = get_page(request, cached, _WEB_SEARCH_PAGE_SIZE)
                return render(
                    request,
                    "dashboard/pages/location/web_search.html",
                    {"search_results": page_obj.object_list, "page_obj": page_obj, "adaptive_pagination": True},
                )

        try:
            search_gateway = get_search_gateway()
            if query := pin.get_unique_search_name():
                search_results = search_gateway.search(query)
            else:
                return render(
                    request,
                    "dashboard/pages/location/web_search.html",
                    {"error": "This pin does not have a descriptive name to search for."},
                )
        except (OSError, ValueError, RuntimeError) as e:
            logger.exception("Unable to contact web search API: %s", e)
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {"error": "Search unavailable. Please try again later."},
            )

        for r in search_results:
            try:
                r["domain"] = urlparse(r.get("link", "")).netloc.removeprefix("www.")
            except (ValueError, AttributeError):
                r["domain"] = ""
            r["date_display"] = format_search_date(r.get("date"))

        if cache_hours > 0:
            cache.set(cache_key, search_results, cache_hours * 3600)

        page_obj = get_page(request, search_results, _WEB_SEARCH_PAGE_SIZE)
        return render(
            request,
            "dashboard/pages/location/web_search.html",
            {"search_results": page_obj.object_list, "page_obj": page_obj, "adaptive_pagination": True},
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
        try:
            pin = Pin.objects.get(slug=kwargs["pin_slug"], profile__user=request.user)
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

        slides: list[SatelliteSlide] = []
        gateways: list[SatelliteViewProvider] = [
            GoogleMapsGateway(api_key=settings.google_unrestricted_api_key or ""),
            EsriGateway(),
            NasaGibsGateway(),
            MapboxGateway(),
            BingMapsGateway(),
            OpenAerialMapGateway(),
        ]
        for gateway in gateways:
            try:
                slides.extend(gateway.get_satellite_slides(lat, lng))
            except RequestCancelledError as rce:
                logger.debug("Satellite view provider %s request cancelled -> %s", gateway.service_key, rce)
            except Exception as e:
                # TODO: Catch specific exceptions
                logger.warning("Satellite view provider %s failed -> %s", gateway.service_key, e)

        return render(
            request,
            "dashboard/pages/location/satellite_view.html",
            {"slides": slides, "lat": lat, "lng": lng, "pin": pin},
        )

    def street_view(self, request: HttpRequest, **kwargs):
        """Returns an HTML fragment with a multi-source street-view carousel.

        Sources included (where available):
        - Google Street View (fetched server-side, cached 30 days)
        - Mapillary crowdsourced imagery (browser-loaded URLs, cached 24 h)
        - KartaView open imagery (browser-loaded URLs, cached 24 h)
        """
        try:
            pin = Pin.objects.get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            return render(request, "dashboard/pages/location/street_view.html", {"error": "No coordinates available."})

        slides: list[StreetViewSlide] = []
        providers: list[StreetViewProvider] = [
            GoogleMapsGateway(api_key=settings.google_unrestricted_api_key or ""),
            MapillaryGateway(),
            KartaViewGateway(),
        ]
        for provider in providers:
            try:
                slides.extend(provider.get_street_view_slides(lat, lng))
            except RequestCancelledError as rce:
                logger.debug("Street view provider %s request cancelled -> %s", provider.service_key, rce)
            except Exception:
                # TODO: Catch specific exceptions
                logger.warning("Street view provider %s failed", provider.__class__.__name__, exc_info=True)

        return render(
            request,
            "dashboard/pages/location/street_view.html",
            {"slides": slides, "pin": pin, "google_maps_api_key": settings.google_unrestricted_api_key},
        )

    @action(detail=True, methods=["get"])
    def import_form(self, request: HttpRequest):
        """
        View the import pins form
        """
        from urbanlens.dashboard.models.badges.model import Badge

        profile = Profile.objects.get(user=request.user)
        tags = Badge.objects.visible_to(profile).ordered()
        return render(
            request,
            "dashboard/pages/location/import/csv.html",
            {
                "form": UploadDataFile(),
                "tags": tags,
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
            except OSError as exc:
                return JsonResponse(
                    {"error": f"Failed to read {uploaded_file.name}: {exc}"},
                    status=400,
                )

            if is_archive(data):
                try:
                    extracted = extract_archive(data)
                except ValueError as exc:
                    return JsonResponse({"error": str(exc)}, status=400)

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

        from urbanlens.dashboard.models.badges.model import Badge

        tag_ids = request.POST.getlist("tag_ids")
        import_tags = list(Badge.objects.visible_to(profile).filter(id__in=tag_ids)) if tag_ids else []
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

        from urbanlens.dashboard.models.badges.model import Badge
        from urbanlens.dashboard.services.archive_extractor import extract_archive, is_archive

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)

        form = UploadDataFile(request.POST, request.FILES)
        if not form.is_valid():
            return JsonResponse({"error": "Invalid form."}, status=400)

        uploaded_files = form.cleaned_data["upload_files"]

        all_files: list[tuple[str, bytes]] = []
        for uploaded_file in uploaded_files:
            try:
                data = uploaded_file.read()
            except OSError as exc:
                return JsonResponse({"error": f"Failed to read {uploaded_file.name}: {exc}"}, status=400)

            if is_archive(data):
                try:
                    extracted = extract_archive(data)
                except ValueError as exc:
                    return JsonResponse({"error": str(exc)}, status=400)
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
        if not lists:
            return JsonResponse({"error": "No valid location files found in the upload."}, status=400)

        badges = Badge.objects.visible_to(profile).ordered()

        return JsonResponse(
            {
                "lists": lists,
                "total": sum(len(lst["pins"]) for lst in lists),
                "badges": [
                    {
                        "id": b.id,
                        "name": b.name,
                        "color": b.color or "",
                        "icon": b.icon or "",
                        "kind": b.kind,
                    }
                    for b in badges
                ],
            },
        )

    # ── External-data HTMX endpoints ───────────────────────────────────────────

    def wikipedia_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Wikipedia article summary for the pin's location.

        Returns an empty 204 when no matching article is found; the client-side
        htmx:afterOnLoad handler removes the loading placeholder on 204.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway

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
            address_components = {
                "locality": location.locality or "",
                "route": location.route or "",
                "street_number": location.street_number or "",
                "administrative_area_level_1": location.administrative_area_level_1 or "",
            }
            try:
                article = WikipediaGateway().get_article_for_location(lat, lng, address_components)
            except Exception:
                logger.exception("Wikipedia lookup failed for pin %s", pin_slug)
                article = None
            LocationCache.set(location, "wikipedia", article or {}, query_key=location.official_name or "")
            data = article
        else:
            data = cached.data or None

        if not data:
            logger.debug("wikipedia_info: no article found for pin %s at (%s, %s)", pin_slug, lat, lng)
            return HttpResponse(status=204)

        return render(request, "dashboard/partials/pins/pin_wikipedia.html", {"article": data})

    def wikimedia_assets(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Wikimedia Commons images for the pin's location name.

        Skipped entirely when the pin has no meaningful name.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.assets.wikimedia import WikimediaGateway

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.meaningful_official_name:
            logger.debug("wikimedia_assets: pin %s has no meaningful official name, skipping", pin_slug)
            return HttpResponse(status=204)

        location = pin.location
        if not location:
            logger.debug("wikimedia_assets: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        query = pin.effective_official_name or ""
        cached = LocationCache.get_fresh(location, "wikimedia")
        if cached is None:
            try:
                images = WikimediaGateway().search_images(query)
            except Exception:
                logger.exception("Wikimedia search failed for pin %s", pin_slug)
                images = []
            LocationCache.set(location, "wikimedia", {"images": images}, query_key=query)
            data = images
        else:
            data = (cached.data or {}).get("images", [])

        if not data:
            logger.debug("wikimedia_assets: no images found for pin %s (query=%r)", pin_slug, query)
            return HttpResponse(status=204)

        page_obj = get_page(request, data, _WIKIMEDIA_PAGE_SIZE)
        return render(
            request,
            "dashboard/partials/pins/pin_wikimedia.html",
            {"images": page_obj.object_list, "page_obj": page_obj, "query": query, "adaptive_pagination": True},
        )

    def loopnet_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: LoopNet commercial real-estate data for the pin's address.

        Requires a full street address; returns 204 when none is available or
        when the search/scrape produces no results.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.real_estate.loopnet import LoopNetGateway

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        location = pin.location
        if not location:
            logger.debug("loopnet_info: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        # Build the address string; skip if we don't have at least street + city
        parts = [
            " ".join(filter(None, [location.street_number, location.route])),
            location.locality or "",
            location.administrative_area_level_1 or "",
        ]
        address = ", ".join(p for p in parts if p).strip(", ")
        if not address or not location.route:
            logger.debug("loopnet_info: pin %s has insufficient address data (route=%r), skipping", pin_slug, location.route)
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "loopnet")
        if cached is None:
            try:
                result = LoopNetGateway().search(address)
            except Exception:
                logger.exception("LoopNet search failed for pin %s", pin_slug)
                result = None
            LocationCache.set(location, "loopnet", result or {}, query_key=address)
            data = result
        else:
            data = cached.data or None

        if not data or not data.get("listings"):
            logger.debug("loopnet_info: no listings found for pin %s (address=%r)", pin_slug, address)
            return HttpResponse(status=204)

        return render(request, "dashboard/partials/pins/pin_loopnet.html", {"result": data, "address": address})

    def nps_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: National Park Service information for the pin's location.

        Looks for a national park whose centre is within 50 km of the pin and
        whose data was retrieved from the NPS API.  Requires an NPS API key.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.parks.nps.parks import NPSGateway

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
        state_code = location.administrative_area_level_1 or ""
        if not lat or not lng or not state_code:
            logger.debug("nps_info: pin %s missing lat/lng or state_code, skipping", pin_slug)
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "nps")
        if cached is None:
            try:
                park = NPSGateway().find_park_near_location(
                    float(lat),
                    float(lng),
                    state_code=state_code,
                    location_name=pin.effective_official_name or "",
                )
            except Exception:
                logger.exception("NPS lookup failed for pin %s", pin_slug)
                park = None
            LocationCache.set(location, "nps", park or {}, query_key=state_code)
            data = park
        else:
            data = cached.data or None

        if not data:
            logger.debug("nps_info: no park found near pin %s (state=%r)", pin_slug, state_code)
            return HttpResponse(status=204)

        return render(request, "dashboard/partials/pins/pin_nps.html", {"park": data})

    def nominatim_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: OpenStreetMap Nominatim place metadata for the pin's location.

        Only renders when at least one useful metadata field is present (website,
        phone, opening hours, operator, or a Wikipedia cross-link).  Returns 204
        for coordinate-only results with no enrichment.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

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
            try:
                place = NominatimGateway().reverse_geocode(float(lat), float(lng))
            except Exception:
                logger.exception("Nominatim lookup failed for pin %s", pin_slug)
                place = None
            LocationCache.set(location, "nominatim", place or {}, query_key=f"{lat},{lng}")
            data = place
        else:
            data = cached.data or None

        useful_fields = ("website", "phone", "opening_hours", "operator", "wikipedia")
        if not data or not any(data.get(k) for k in useful_fields):
            logger.debug("nominatim_info: no enrichment data for pin %s at (%s, %s)", pin_slug, lat, lng)
            return HttpResponse(status=204)

        return render(request, "dashboard/partials/pins/pin_nominatim.html", {"place": data})

    def loc_info(self, request: HttpRequest, pin_slug: str):
        """HTMX partial: Library of Congress records for the pin's location.

        Only queries USA-based locations. Returns 204 when the pin has no
        meaningful name, is outside the USA, or when no results are found.
        Results are cached in LocationCache for 7 days.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.assets.loc import LOCJsonGateway
        from urbanlens.dashboard.services.geo_filter import is_usa_coordinates

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not is_usa_coordinates(pin.effective_latitude, pin.effective_longitude):
            return HttpResponse(status=204)

        query = pin.get_unique_search_name(include_country=False)
        if not query:
            return HttpResponse(status=204)

        location = pin.location
        if not location:
            return HttpResponse(status=204)

        cached = LocationCache.get_fresh(location, "loc")
        if cached is None:
            try:
                results = LOCJsonGateway().search(query)
            except Exception:
                logger.exception("LOC search failed for pin %s", pin_slug)
                results = []
            LocationCache.set(location, "loc", {"results": results}, query_key=query)
            data = results
        else:
            data = (cached.data or {}).get("results", [])

        if not data:
            return HttpResponse(status=204)

        page_obj = get_page(request, data, 8)
        return render(
            request,
            "dashboard/partials/pins/pin_loc.html",
            {"results": page_obj.object_list, "page_obj": page_obj, "query": query, "adaptive_pagination": True},
        )

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
            try:
                result = UsgsGateway().historical_topo_maps_for_coordinates(float(lat), float(lng), delta=0.01)
            except Exception:
                logger.exception("USGS topo lookup failed for pin %s", pin_slug)
                result = None
            LocationCache.set(location, "usgs_topo", result or {}, query_key=f"{float(lat):.4f},{float(lng):.4f}")
            data = result
        else:
            data = cached.data or None

        maps_list = (data or {}).get("items") or []
        if not maps_list:
            logger.debug("usgs_topo_info: no topo maps found for pin %s", pin_slug)
            return HttpResponse(status=204)

        return render(request, "dashboard/partials/pins/pin_usgs_topo.html", {"maps": maps_list[:20]})

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
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(slug=pin_slug)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        if not pin.latitude or not pin.longitude:
            return HttpResponse("Pin does not have valid coordinates", status=400)

        from urbanlens.dashboard.services.apis.weather.gateway import OpenWeatherMapGateway

        # Instantiate the OpenWeatherMapGateway with the API key
        weather_forecast_gateway = OpenWeatherMapGateway()

        # Get the weather forecast from the OpenWeather API
        weather_forecast = weather_forecast_gateway.get_weather_forecast(pin.latitude, pin.longitude)

        logger.debug("forecast_data: %s", weather_forecast)

        return render(request, "dashboard/pages/location/weather.html", {"forecast": weather_forecast})
