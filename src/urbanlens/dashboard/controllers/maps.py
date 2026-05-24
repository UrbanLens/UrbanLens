"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        File:    MapController.py                                                                                     *
*        Path:    /dashboard/controllers/map.py                                                                        *
*        Project: urbanlens                                                                                            *
*        Version: 0.0.2                                                                                                *
*        Created: 2023-12-24                                                                                           *
*        Author:  Jess Mann                                                                                            *
*        Email:   jess@urbanlens.org                                                                                 *
*        Copyright (c) 2025 Jess Mann                                                                                  *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2023-12-24     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from datetime import datetime
import logging

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.forms.advanced_search import AdvancedSearchForm
from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.categories.model import Category
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin import Pin, PinQuerySet
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.tags.model import Tag
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class MapController(LoginRequiredMixin, GenericViewSet):
    def view_map(self, request, *args, **kwargs):
        from urbanlens.dashboard.models.pin.model import PinStatus

        profile, _ = Profile.objects.get_or_create(user=request.user)
        tags = Tag.objects.filter(profile=profile).order_by("order", "name")
        return render(
            request,
            "dashboard/pages/map/index.html",
            {
                "openweathermap_api_key": settings.openweathermap_api_key,
                "tags": tags,
                "status_choices": PinStatus.choices,
            },
        )

    def edit_pin(self, request, pin_uuid, *args, **kwargs):
        pin: Pin = Pin.objects.get(uuid=pin_uuid)
        # Update the pin based on the form data
        pin.nickname = request.POST.get("name")
        pin.description = request.POST.get("description")
        pin.latitude = request.POST.get("latitude")
        pin.longitude = request.POST.get("longitude")
        tags = request.POST.get("tags").split(",")
        for tag_name in tags:
            tag, _created = Tag.objects.get_or_create(name=tag_name)
            pin.tags.add(tag)
        icon = request.FILES.get("icon", None)
        if icon:
            pin.icon = icon
        pin.save()
        return HttpResponseRedirect(reverse("map.view"))

    def get_edit_pin(self, request, pin_uuid, *args, **kwargs):
        pin = Pin.objects.get(uuid=pin_uuid)
        # Render the edit form
        categories = Category.objects.all()
        return render(request, "dashboard/pages/map/edit_location.html", {"pin": pin, "categories": categories})

    def add_pin(self, request, *args, **kwargs):
        # Render the add form
        return render(request, "dashboard/pages/map/add_location.html")

    def post_add_pin(self, request, *args, **kwargs):
        try:
            name = request.POST.get("name")
            latitude = request.POST.get("latitude")
            longitude = request.POST.get("longitude")
            address = request.POST.get("address", None)
            tags = request.POST.get("tags")
            tags = tags.split(",") if tags else []
            icon = request.POST.get("icon", None)

            if not latitude or not longitude:
                if not address:
                    return HttpResponse("Error: No address or lat/lon provided.", status=400)
                latitude, longitude = get_pin_by_address(address)
                if not latitude or not longitude:
                    return HttpResponse("Error: Unable to convert address to lat/lng.", status=400)

            lat_f = float(latitude)
            lon_f = float(longitude)

            # Link to an existing Location whose bounding box contains this point,
            # or create a new one. This keeps all pins for the same place connected.
            location = Location.objects.get_for_point(lat_f, lon_f)
            if not location:
                location = Location.objects.create(
                    name=name or "Unnamed Location",
                    latitude=lat_f,
                    longitude=lon_f,
                )

            pin = Pin.objects.create(
                nickname=name,
                location=location,
                # Only store coordinate override when it differs from the location.
                # For a brand-new location the pin is the source of truth, so clear overrides.
                latitude=None,
                longitude=None,
                icon=icon,
                profile=request.user.profile,
            )
            for tag_name in tags:
                if tag_name.strip():
                    tag, _ = Tag.objects.get_or_create(name=tag_name.strip())
                    pin.tags.add(tag)
            pin.save()
            return HttpResponse(status=200)
        except Exception as e:
            logger.exception("Failed to create pin: %s", e)
            return HttpResponse(f"Error: {e!s}", status=400)

    def search_map(self, request, *args, **kwargs):
        search_form = SearchForm()
        return render(request, "dashboard/pages/map/search.html", {"form": search_form})

    def search_map_post(self, request, *args, **kwargs):
        logger.info("Searching map...")
        search_form = SearchForm(request.POST)
        if search_form.is_valid():
            profile, _ = Profile.objects.get_or_create(user=request.user)
            query = Pin.objects.filter(profile=profile).filter_by_criteria(search_form.cleaned_data)
            map_data = self.get_map_data(request, query)
            return render(request, "dashboard/pages/map/data.html", {"map_data": map_data})

        logger.error("Invalid search criteria: %s", search_form.errors)
        return HttpResponse(status=400, content="Invalid search criteria.")

    def upload_image(self, request, pin_uuid, *args, **kwargs):
        image = request.FILES.get("image")
        pin = Pin.objects.get(uuid=pin_uuid)
        Image.objects.create(image=image, pin=pin)
        return HttpResponse(status=200)

    def change_category(self, request, pin_uuid, *args, **kwargs):
        category_id = request.POST.get("category")
        pin = Pin.objects.get(uuid=pin_uuid)
        pin.change_category(category_id)
        return HttpResponseRedirect(reverse("view_map"))

    def post_advanced_search(self, request, *args, **kwargs):
        form = AdvancedSearchForm(request.POST)
        if form.is_valid():
            pins = Pin.objects.all().filter_by_criteria(form.cleaned_data)
            return render(request, "dashboard/pages/map/index.html", {"pins": pins})
        return None

    def get_advanced_search(self, request, *args, **kwargs):
        form = AdvancedSearchForm()
        return render(request, "dashboard/pages/map/advanced_search.html", {"form": form})

    def map_pins_json(self, request, *args, **kwargs):
        """Return pin data as JSON with optional bbox filtering for two-phase map loading.

        Query params:
            bbox: "south,west,north,east" floats - restrict to this bounding box.
        """
        from django.contrib.gis.geos import Polygon

        profile, _ = Profile.objects.get_or_create(user=request.user)
        query = Pin.objects.filter(profile=profile).root_pins().select_related("location").prefetch_related("tags")

        bbox_str = request.GET.get("bbox", "").strip()
        if bbox_str:
            try:
                parts = [float(x) for x in bbox_str.split(",")]
                if len(parts) == 4:
                    south, west, north, east = parts
                    bbox_poly = Polygon.from_bbox((west, south, east, north))
                    bbox_poly.srid = 4326
                    query = query.filter(point__within=bbox_poly)
            except Exception as e:
                logger.warning("Invalid bbox parameter: %s -> %s", bbox_str, e)

        map_data = self.get_map_data(request, query)
        for pin_dict in map_data:
            pin_dict["viewLocationUrl"] = f"/dashboard/map/pin/{pin_dict['uuid']}/"

        return JsonResponse({"pins": map_data})

    def init_map(self, request, *args, **kwargs):
        map_data = self.get_map_data(request)

        return render(request, "dashboard/pages/map/data.html", {"map_data": map_data})

    def get_map_data(self, request, query: PinQuerySet | None = None):
        if query is None:
            profile, _ = Profile.objects.get_or_create(user=request.user)
            query = Pin.objects.filter(profile=profile).root_pins().select_related("location")

        query = query.prefetch_related("tags")

        if not query:
            # Default map data
            map_data = []  # {'latitude': 42.65250213448323, 'longitude': -73.75791867436858, 'name': 'Default Pin', 'description': 'No pins saved yet.'}]
        else:
            map_data = [pin.to_json() for pin in query]

        for pin in map_data:
            if "description" in pin and pin["description"] is None:
                pin["description"] = ""

            # Turn arrays into csv
            if pin.get("tags"):
                tags = pin["tags"]
                if tags and isinstance(tags[0], dict):
                    pin["tags"] = ", ".join(t["name"] for t in tags)
                else:
                    pin["tags"] = ", ".join(tags)
            else:
                pin["tags"] = ""
            if pin.get("categories"):
                pin["categories"] = ", ".join(pin["categories"])
            else:
                pin["categories"] = ""

            # Last visited = None => Never
            if "last_visited" not in pin or not pin["last_visited"] or pin["last_visited"] == "never":
                pin["last_visited"] = "Never"
            else:
                try:
                    # Dates look like this: 2023-01-02T00:00:00+00:00
                    pin["last_visited"] = datetime.strptime(pin["last_visited"], "%Y-%m-%dT%H:%M:%S%z").strftime(
                        "%Y-%m-%d",
                    )
                except ValueError:
                    logger.warning("Unable to parse date: %s", pin["last_visited"])

            if pin.get("status"):
                pin["status"] = pin["status"].replace("_", " ").capitalize()

        return map_data


@login_required
def get_pin_by_address(address):
    try:
        geolocator = Nominatim(user_agent="geoapiExercises")
        pin = geolocator.geocode(address)
        if pin:
            return (pin.latitude, pin.longitude)

    except GeocoderTimedOut:
        logger.exception("Geocoder service timed out.")
        raise
    except GeocoderUnavailable:
        logger.exception("Geocoder service unavailable.")
        raise
    return (None, None)
