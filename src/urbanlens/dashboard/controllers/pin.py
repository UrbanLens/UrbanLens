from datetime import UTC, datetime
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from requests.exceptions import HTTPError
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.forms.upload_datafile import UploadDataFile
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.profile import Profile
from urbanlens.dashboard.services.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.search import get_search_gateway
from urbanlens.dashboard.services.smithsonian import SmithsonianGateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


def _format_search_date(raw: str | None) -> str:
    """Convert an ISO date string or human-readable age string to a short display label."""
    if not raw:
        return ""
    from datetime import datetime, timezone

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:19].rstrip("Z"), fmt.rstrip("%z"))
            dt = dt.replace(tzinfo=UTC)
            now = datetime.now(tz=UTC)
            delta = now - dt
            if delta.days < 1:
                hours = delta.seconds // 3600
                return f"{hours}h ago" if hours else "Just now"
            if delta.days < 7:
                return f"{delta.days}d ago"
            if delta.days < 365:
                return dt.strftime("%b %-d")
            return dt.strftime("%b %-d, %Y")
        except ValueError:
            continue
    return raw


def _build_pin_search_query(pin: Pin) -> str:
    """Build a search query combining the pin's name with optional location keywords.

    The effective name and place name are the primary search terms. Street name,
    city, and state are appended as comma-separated optional keywords so that
    search engines can disambiguate results without requiring an exact phrase match.
    """
    name = pin.effective_name
    place_name = getattr(pin, "place_name", None)
    address_basic = pin.address_basic
    route = pin.location.route if pin.location else None

    # Primary identifier(s)
    primary: list[str] = []
    if name:
        primary.append(name)
    if place_name and place_name not in {name, address_basic}:
        primary.append(place_name)
    elif address_basic and address_basic != name:
        primary.append(address_basic)

    if not primary:
        if pin.effective_latitude is not None and pin.effective_longitude is not None:
            return f"{pin.effective_latitude}, {pin.effective_longitude}"
        return ""

    primary_str = " ".join(primary)

    # Optional location keywords: street name, city (or county), state
    location: list[str] = []
    if route and route not in primary_str:
        location.append(route)
    if pin.city:
        location.append(pin.city)
    elif pin.county:
        location.append(pin.county)
    if pin.state:
        location.append(pin.state)

    return ", ".join(filter(None, [primary_str, *location]))


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

    def test_ai(self, request: HttpRequest, *args, **kwargs):
        """
        Test the AI. TODO Temporary function that can be deleted at any time with no side effects.
        """
        profile = Profile.objects.get(pk=1)
        pin, _created = Pin.objects.get_nearby_or_create(
            latitude=43.0423439,
            longitude=-76.1501928,
            profile=profile,
            defaults={
                "nickname": "Syracuse Central High School",
                "description": "",
            },
        )
        logger.critical("Location: %s", pin)
        return JsonResponse({"pin": pin.to_json()})

        from urbanlens.dashboard.services.ai.cloudflare import CloudflareGateway

        instructions = (
            ""
            + "Look at the following information about a location and determine what category it belongs in. Example categories are:"
            + "Airport, Amusement Park, Asylum, Bank, Bridge, Bunker, Cars, Castle, Church, Factory, Firehouse, Fire Tower, "
            + "Funeral Home, Graveyard, Hospital, Hotel, House, Laboratory, Library, Lighthouse, Mall, Mansion, Military Base, "
            + "Monument, Police Station, Power Plant, Prison, Resort, Ruins, School, Stadium, Theater, Traincar, Train Station, Tunnel"
            + "If the Pin does not fit into any of these categories, provide a new category that is broad enough to include a variety "
            + "of similar urbex locations. Do not answer with the name of the location; always answer with a category, like this: <ANSWER>Factory</ANSWER>."
        )

        gateway = CloudflareGateway(instructions=instructions)
        response = gateway.send_prompt("address: 312 Western Ave, Guilderland, NY 12084, USA, name: Master Cleaners")

        return JsonResponse({"response": response})

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

        if not pin.has_meaningful_name:
            return HttpResponse("", status=204)

        cache_key = f"web_search_{pin_slug}"
        site_settings = SiteSettings.get_current()
        cache_hours = site_settings.search_cache_hours

        if cache_hours > 0:
            cached = cache.get(cache_key)
            if cached is not None:
                return render(request, "dashboard/pages/location/web_search.html", {"search_results": cached})

        try:
            search_gateway = get_search_gateway()
            query = _build_pin_search_query(pin)
            search_results = search_gateway.search(query)
        except Exception as e:
            logger.exception("Unable to contact web search API: %s", e)
            return render(
                request,
                "dashboard/pages/location/web_search.html",
                {"error": "Search unavailable. Please try again later."},
            )

        for r in search_results:
            try:
                r["domain"] = urlparse(r.get("link", "")).netloc.removeprefix("www.")
            except Exception:
                r["domain"] = ""
            r["date_display"] = _format_search_date(r.get("date"))

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

        try:
            pin = Pin.objects.get(slug=kwargs["pin_slug"], profile__user=request.user)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            return render(request, "dashboard/pages/location/street_view.html", {"error": "No coordinates available."})

        try:
            google_maps_gateway = GoogleMapsGateway(api_key=settings.google_street_view_api_key or "")
            image_bytes, capture_date = google_maps_gateway.get_street_view(lat, lng)
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
        except Exception as exc:
            logger.warning("Street view unavailable for pin %s: %s", kwargs.get("pin_slug"), exc)
            return render(request, "dashboard/pages/location/street_view.html", {"error": "Street view unavailable."})

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
            except Exception as exc:
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
            except Exception as exc:
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

    @action(detail=False, methods=["post"])
    def import_confirmed(self, request: HttpRequest):
        """Stream SSE import progress for user-confirmed pin selections from the preview step."""
        import json as _json

        if not isinstance(request.user, User):
            return JsonResponse({"error": "Authentication required."}, status=401)

        try:
            payload = request.data
            confirmed_lists = payload.get("lists", [])
        except (ValueError, KeyError):
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)

        if not confirmed_lists:
            return JsonResponse({"error": "No lists provided."}, status=400)

        profile, _ = Profile.objects.get_or_create(user=request.user)
        gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")

        response = StreamingHttpResponse(
            gateway.import_preview_streaming(confirmed_lists, profile),
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
