from datetime import datetime
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from requests.exceptions import HTTPError
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.viewsets import GenericViewSet

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.forms.upload_datafile import UploadDataFile
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.services.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.search import build_pin_search_query, format_search_date, get_search_gateway
from urbanlens.dashboard.services.smithsonian import SmithsonianGateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


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
                "google_maps_api_key": settings.google_maps_api_key,
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

        # Instantiate the SmithsonianGateway with the API key
        smithsonian_gateway = SmithsonianGateway(api_key=settings.smithsonian_api_key or "")

        # Get historic images from the Smithsonian's API; discard entries without a usable URL
        smithsonian_images = [img for img in smithsonian_gateway.get_data(pin.effective_name) if img.get("url")]

        return render(
            request,
            "dashboard/pages/location/smithsonian.html",
            {
                "images": smithsonian_images,
            },
        )

    def web_search(self, request: HttpRequest, pin_slug):
        """
        Returns the web search results for a pin.
        """
        from urllib.parse import urlparse

        from django.core.cache import cache

        from urbanlens.dashboard.models.site_settings import SiteSettings

        try:
            pin: Pin = Pin.objects.get(slug=pin_slug)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        if not pin.meaningful_name:
            return HttpResponse("", status=204)

        cache_key = make_cache_key("web_search_pin", str(pin.pk))
        site_settings = SiteSettings.get_current()
        cache_hours = site_settings.search_cache_hours

        if cache_hours > 0:
            cached = cache.get(cache_key)
            if cached is not None:
                return render(request, "dashboard/pages/location/web_search.html", {"search_results": cached})

        try:
            search_gateway = get_search_gateway()
            query = build_pin_search_query(pin)
            search_results = search_gateway.search(query)
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

        return render(request, "dashboard/pages/location/web_search.html", {"search_results": search_results})

    def satellite_view_google_image(self, request: HttpRequest, **kwargs):
        """
        Returns an HTML fragment with an Esri satellite map for a pin.
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

        return render(request, "dashboard/pages/location/satellite_view.html", {"lat": lat, "lng": lng, "pin": pin})

    def street_view(self, request: HttpRequest, **kwargs):
        """
        Returns an HTML fragment containing the street view image for a pin.
        """
        import base64

        from django.core.cache import cache

        try:
            pin = Pin.objects.get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            return render(request, "dashboard/pages/location/street_view.html", {"error": "No coordinates available."})

        thirty_days = 30 * 24 * 3600
        cache_key = make_cache_key("street_view", f"{float(lat):.6f}", f"{float(lng):.6f}")
        cached = cache.get(cache_key)
        if cached is not None:
            image_b64, capture_date = cached
            return render(
                request,
                "dashboard/pages/location/street_view.html",
                {"image_b64": image_b64, "pin": pin, "capture_date": capture_date},
            )

        try:
            google_maps_gateway = GoogleMapsGateway(api_key=settings.google_street_view_api_key or "")
            image_bytes, capture_date = google_maps_gateway.get_street_view(lat, lng)
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Street view unavailable for pin %s: %s", kwargs.get("pin_slug"), exc)
            return render(request, "dashboard/pages/location/street_view.html", {"error": "Street view unavailable."})

        cache.set(cache_key, (image_b64, capture_date), thirty_days)

        return render(
            request,
            "dashboard/pages/location/street_view.html",
            {"image_b64": image_b64, "pin": pin, "capture_date": capture_date},
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

        google_maps_gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")

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
        gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")

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
        from urbanlens.dashboard.services.wikipedia import WikipediaGateway

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
            LocationCache.set(location, "wikipedia", article or {}, query_key=location.name or "")
            data = article
        else:
            data = cached.data or None

        if not data:
            logger.debug("wikipedia_info: no article found for pin %s at (%s, %s)", pin_slug, lat, lng)
            return HttpResponse(status=204)

        return render(request, "dashboard/partials/pin_wikipedia.html", {"article": data})

    def wikimedia_assets(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: Wikimedia Commons images for the pin's location name.

        Skipped entirely when the pin has no meaningful name.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.wikimedia import WikimediaGateway

        try:
            pin = Pin.objects.select_related("location").get(slug=pin_slug, profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse(status=404)

        if not pin.meaningful_name:
            logger.debug("wikimedia_assets: pin %s has no meaningful name, skipping", pin_slug)
            return HttpResponse(status=204)

        location = pin.location
        if not location:
            logger.debug("wikimedia_assets: pin %s has no location, skipping", pin_slug)
            return HttpResponse(status=204)

        query = pin.effective_name or ""
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

        return render(request, "dashboard/partials/pin_wikimedia.html", {"images": data, "query": query})

    def loopnet_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: LoopNet commercial real-estate data for the pin's address.

        Requires a full street address; returns 204 when none is available or
        when the search/scrape produces no results.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.loopnet import LoopNetGateway

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

        return render(request, "dashboard/partials/pin_loopnet.html", {"result": data, "address": address})

    def nps_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: National Park Service information for the pin's location.

        Looks for a national park whose centre is within 50 km of the pin and
        whose data was retrieved from the NPS API.  Requires an NPS API key.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.nps.parks import NPSGateway

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
                    location_name=pin.effective_name or "",
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

        return render(request, "dashboard/partials/pin_nps.html", {"park": data})

    def nominatim_info(self, request: HttpRequest, pin_slug: str):
        """
        HTMX partial: OpenStreetMap Nominatim place metadata for the pin's location.

        Only renders when at least one useful metadata field is present (website,
        phone, opening hours, operator, or a Wikipedia cross-link).  Returns 204
        for coordinate-only results with no enrichment.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.nominatim import NominatimGateway

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

        return render(request, "dashboard/partials/pin_nominatim.html", {"place": data})

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
        gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")

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

        from urbanlens.dashboard.services.openweather.gateway import WeatherForecastGateway

        # Instantiate the WeatherForecastGateway with the API key
        weather_forecast_gateway = WeatherForecastGateway()

        # Get the weather forecast from the OpenWeather API
        weather_forecast = weather_forecast_gateway.get_weather_forecast(pin.latitude, pin.longitude)

        logger.debug("forecast_data: %s", weather_forecast)

        return render(request, "dashboard/pages/location/weather.html", {"forecast": weather_forecast})
