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
        pin = Pin.objects.get(id=kwargs["pin_id"])

        return render(
            request,
            "dashboard/pages/location/index.html",
            {"pin": pin, "google_maps_api_key": settings.google_maps_api_key},
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

    def get_smithsonian_images(self, request: HttpRequest, pin_id):
        """
        Returns the Smithsonian images for a pin.
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(id=pin_id)
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

    def web_search(self, request: HttpRequest, pin_id):
        """
        Returns the web search results for a pin.
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(id=pin_id)
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
        except HTTPError as e:
            logger.exception("Unable to contact Google Search API. Is the API Key valid? Exception ---> %s", e)
            return HttpResponse("Unable to search. This is unlikely to be resolved by multiple requests.", status=500)

        return render(request, "dashboard/pages/location/web_search.html", {"search_results": search_results})

    def satellite_view_google_image(self, request: HttpRequest, **kwargs):
        """
        Returns the satellite view image for a pin.
        """
        try:
            pin = Pin.objects.get(id=kwargs["pin_id"])
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the GoogleMapsGateway with the API key
        google_maps_gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")

        # Get the satellite view image from the Google Maps API
        satellite_image = google_maps_gateway.get_satellite_view(pin.latitude, pin.longitude)

        return HttpResponse(satellite_image, content_type="image/jpeg")

    def street_view(self, request: HttpRequest, **kwargs):
        """
        Returns the street view image for a pin.
        """
        try:
            pin = Pin.objects.get(id=kwargs["pin_id"])
        except Pin.DoesNotExist:
            return HttpResponse("Pin does not exist", status=404)

        # Instantiate the GoogleMapsGateway with the API key
        google_maps_gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")

        # Get the street view image from the Google Maps API
        street_view_image = google_maps_gateway.get_street_view(pin.latitude, pin.longitude)

        return HttpResponse(street_view_image, content_type="image/jpeg")

    @action(detail=True, methods=["get"])
    def import_form(self, request: HttpRequest):
        """
        View the import pins form
        """
        from urbanlens.dashboard.models.tags.model import Tag

        profile = Profile.objects.get(user=request.user)
        tags = Tag.objects.visible_to(profile).ordered()
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

        from urbanlens.dashboard.models.tags.model import Tag

        tag_ids = request.POST.getlist("tag_ids")
        import_tags = list(Tag.objects.visible_to(profile).filter(id__in=tag_ids)) if tag_ids else []

        google_maps_gateway = GoogleMapsGateway(api_key=settings.google_maps_api_key or "")

        response = StreamingHttpResponse(
            google_maps_gateway.import_pins_streaming(all_files, profile, tags=import_tags),
            content_type="text/event-stream",
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response

    def weather_forecast(self, request: HttpRequest, pin_id):
        """
        Returns the weather forecast for a pin.
        """
        # Get the pin
        try:
            pin: Pin = Pin.objects.get(id=pin_id)
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
