"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    pin.py                                                                                        *
*        - Path:    /dashboard/controllers/pin.py                                                                 *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-01-01                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-22     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from datetime import datetime
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
from urbanlens.dashboard.services.google.search import GoogleCustomSearchGateway
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
        from urbanlens.dashboard.models.pin.model import PinStatus, PinType

        pin = Pin.objects.select_related("location").get(uuid=kwargs["pin_uuid"])

        # Auto-link legacy pins that pre-date the Location requirement.
        if pin.location is None and pin.effective_latitude and pin.effective_longitude:
            lat, lon = pin.effective_latitude, pin.effective_longitude
            location = Location.objects.get_for_point(lat, lon)
            if not location:
                location = Location.objects.create(
                    name=pin.effective_name or "Unnamed Location",
                    latitude=lat,
                    longitude=lon,
                )
            pin.location = location
            pin.save(update_fields=["location"])

        profile, _ = Profile.objects.get_or_create(user=request.user)

        today = date.today()
        min_date = date(today.year - 100, today.month, today.day)

        detail_pin_icon_choices = [
            ("place", "Place"), ("business", "Building"), ("door_front", "Entrance"),
            ("star", "Star"), ("warning", "Warning"), ("info", "Info"),
            ("camera_alt", "Camera"), ("local_parking", "Parking"),
            ("stairs", "Stairs"), ("elevator", "Elevator"),
            ("exit_to_app", "Exit"), ("lock", "Lock"),
            ("construction", "Construction"), ("emergency", "Emergency"),
        ]

        return render(
            request,
            "dashboard/pages/location/index.html",
            {
                "pin": pin,
                "google_maps_api_key": settings.google_maps_api_key,
                "openweathermap_api_key": settings.openweathermap_api_key,
                "page_name": "location-details",
                "pin_status_choices": PinStatus.choices,
                "pin_type_choices": PinType.choices,
                "detail_pin_icon_choices": detail_pin_icon_choices,
                "color_choices": COLOR_CHOICES,
                "all_categories": Badge.objects.categories().ordered(),
                "default_map_view": profile.default_map_view,
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

    def get_smithsonian_images(self, request: HttpRequest, pin_uuid):
        """
        Returns the Smithsonian images for a pin.
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(uuid=pin_uuid)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the SmithsonianGateway with the API key
        smithsonian_gateway = SmithsonianGateway(api_key=settings.smithsonian_api_key or "")

        # Get historic images from the Smithsonian's API
        smithsonian_images = smithsonian_gateway.get_data(pin.effective_name)

        return render(
            request,
            "dashboard/pages/location/smithsonian.html",
            {
                "images": smithsonian_images,
            },
        )

    def web_search(self, request: HttpRequest, pin_uuid):
        """
        Returns the web search results for a pin.
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(uuid=pin_uuid)
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the GoogleCustomSearchGateway with the API key
        try:
            google_gateway = GoogleCustomSearchGateway()

            # Get web search results from the Google Custom Search API
            query = [
                pin.address_extended,
                [
                    pin.address_basic,
                    pin.city,
                ],
                [
                    pin.address_basic,
                    pin.county,
                ],
                [
                    pin.address_basic,
                    pin.state,
                ],
                f"{pin.latitude}, {pin.longitude}",
            ]

            if pin.effective_name and pin.address_basic != pin.effective_name:
                query.append(
                    [
                        pin.effective_name,
                        pin.city,
                    ],
                )

            place_name = pin.place_name
            if place_name and place_name not in {pin.address_basic, pin.effective_name}:
                query.append(place_name)

            search_results = google_gateway.search(query)
        except Exception as e:
            logger.exception("Unable to contact Google Search API: %s", e)
            return render(request, "dashboard/pages/location/web_search.html", {"error": "Search unavailable. Please try again later."})

        return render(request, "dashboard/pages/location/web_search.html", {"search_results": search_results})

    def satellite_view_google_image(self, request: HttpRequest, **kwargs):
        """
        Returns an HTML fragment containing the satellite view image for a pin.
        """
        import base64

        try:
            pin = Pin.objects.get(uuid=kwargs["pin_uuid"])
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            return render(request, "dashboard/pages/location/satellite_view.html", {"error": "No coordinates available."})

        try:
            google_maps_gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")
            image_bytes = google_maps_gateway.get_satellite_view(lat, lng)
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
        except Exception as exc:
            logger.warning("Satellite view unavailable for pin %s: %s", kwargs.get("pin_uuid"), exc)
            return render(request, "dashboard/pages/location/satellite_view.html", {"error": "Satellite image unavailable."})

        return render(request, "dashboard/pages/location/satellite_view.html", {"image_b64": image_b64, "pin": pin})

    def street_view(self, request: HttpRequest, **kwargs):
        """
        Returns an HTML fragment containing the street view image for a pin.
        """
        import base64

        try:
            pin = Pin.objects.get(uuid=kwargs["pin_uuid"])
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            return render(request, "dashboard/pages/location/street_view.html", {"error": "No coordinates available."})

        try:
            google_maps_gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")
            image_bytes = google_maps_gateway.get_street_view(lat, lng)
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
        except Exception as exc:
            logger.warning("Street view unavailable for pin %s: %s", kwargs.get("pin_uuid"), exc)
            return render(request, "dashboard/pages/location/street_view.html", {"error": "Street view unavailable."})

        return render(request, "dashboard/pages/location/street_view.html", {"image_b64": image_b64, "pin": pin})

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
            google_maps_gateway.import_pins_streaming(all_files, profile, tags=import_tags, tag_by_filename=tag_by_filename),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def weather_forecast(self, request: HttpRequest, pin_uuid):
        """
        Returns the weather forecast for a pin.
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(uuid=pin_uuid)
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
