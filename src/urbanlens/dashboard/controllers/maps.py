from datetime import datetime
import json
import logging
from typing import Any

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from geopy.geocoders import Nominatim
from rest_framework.viewsets import GenericViewSet

from urbanlens.dashboard.forms.advanced_search import AdvancedSearchForm
from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin import Pin, PinQuerySet
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.map_pins import MapPinCache, MapPinPayloadService
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class MapController(LoginRequiredMixin, GenericViewSet):
    def view_map(self, request, *args, **kwargs):
        from urbanlens.dashboard.models.profile.model import MapCenterMode

        profile, _ = Profile.objects.get_or_create(user=request.user)
        tags = Badge.objects.tags().visible_to(profile).ordered()
        categories = Badge.objects.categories().ordered()
        from urbanlens.dashboard.models.badges.model import KIND_USER

        filter_badges = Badge.objects.exclude(kind=KIND_USER).visible_to(profile).ordered()
        pin_count = Pin.objects.filter(profile=profile).root_pins().count()

        site = SiteSettings.get_current()
        show_pin_count = site.show_dev_admin_features(request.user)
        return render(
            request,
            "dashboard/pages/map/index.html",
            {
                "openweathermap_api_key": settings.openweathermap_api_key,
                "tags": tags,
                "categories": categories,
                "filter_badges": filter_badges,
                "profile_id": profile.id,
                "profile_slug": profile.slug or str(profile.uuid),
                "app_uuid": str(site.instance_uuid),
                "cluster_radius": profile.cluster_radius,
                "pin_count": pin_count,
                "show_pin_count": show_pin_count,
                "use_pin_cache": profile.use_pin_cache,
                **profile.get_map_center_template_context(),
                "map_default_zoom": (
                    profile.remembered_map_zoom
                    if profile.map_center_mode == MapCenterMode.REMEMBER and profile.remembered_map_zoom
                    else profile.map_default_zoom or 13
                ),
                "default_map_view": profile.default_map_view,
                "map_dark_mode": profile.map_dark_mode,
            },
        )

    def edit_pin(self, request, pin_slug, *args, **kwargs):
        pin: Pin = Pin.objects.get(slug=pin_slug)
        # Update the pin based on the form data
        pin.name = request.POST.get("name")
        pin.description = request.POST.get("description")
        pin.latitude = request.POST.get("latitude")
        pin.longitude = request.POST.get("longitude")
        tags = request.POST.get("tags").split(",")
        for tag_name in tags:
            tag, _created = Badge.objects.get_or_create(name=tag_name)
            pin.badges.add(tag)
        icon = request.FILES.get("icon", None)
        if icon:
            pin.icon = icon
        pin.save()
        return HttpResponseRedirect(reverse("map.view"))

    def get_edit_pin(self, request, pin_slug, *args, **kwargs):
        pin = Pin.objects.get(slug=pin_slug)
        # Render the edit form
        categories = Badge.objects.categories().ordered()
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
            icon = request.POST.get("icon", None)
            tag_ids = request.POST.getlist("tag_ids")
            category_ids = request.POST.getlist("category_ids")
            is_private = request.POST.get("is_private") in {"1", "true", "on", "True"}

            if not latitude or not longitude:
                if not address:
                    return HttpResponse("Error: No address or lat/lon provided.", status=400)
                latitude, longitude = get_pin_by_address(address)
                if not latitude or not longitude:
                    return HttpResponse("Error: Unable to convert address to lat/lng.", status=400)

            lat_f = float(latitude)
            lon_f = float(longitude)

            location = None
            all_locations: list[Location] = []

            if not is_private:
                # Link to an existing Location whose bounding box contains this point,
                # or create a new one. This keeps all pins for the same place connected.
                # The Location name must be the canonical place name - never the user's
                # custom label, which stays on Pin.name only.
                all_locations = list(Location.objects.get_all_for_point(lat_f, lon_f))
                if all_locations:
                    location = all_locations[0]
                else:
                    location = _create_location_with_canonical_name(lat_f, lon_f)

            pin = Pin.objects.create(
                name=name,
                location=location,
                latitude=None,
                longitude=None,
                icon=icon,
                is_private=is_private,
                profile=request.user.profile,
            )
            if tag_ids:
                pin.badges.remove(*pin.badges.filter(kind="tag"))
                pin.badges.add(*Badge.objects.tags().filter(id__in=tag_ids))
            if category_ids:
                pin.badges.remove(*pin.badges.filter(kind="category"))
                pin.badges.add(*Badge.objects.categories().filter(id__in=category_ids))
            pin.save()

            from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

            if user_has_feature(request.user, SiteFeature.AI):
                from urbanlens.dashboard.services.celery import safely_enqueue_task
                from urbanlens.dashboard.tasks import suggest_pin_category

                safely_enqueue_task(suggest_pin_category, pin.pk)

            response = {"ok": True, "pin_slug": pin.slug or str(pin.uuid)}
            # When a coordinate falls inside multiple bounding boxes, tell the
            # client so it can offer the user a choice of which location to use.
            if len(all_locations) > 1:
                from django.urls import reverse

                response["conflicting_locations"] = [
                    {
                        "uuid": str(loc.uuid),
                        "slug": loc.slug or str(loc.uuid),
                        "name": loc.name,
                        "is_current": loc.pk == location.pk,
                        "wiki_url": reverse("location.wiki", kwargs={"location_slug": loc.slug or str(loc.uuid)}),
                    }
                    for loc in all_locations
                ]
            from django.http import JsonResponse

            return JsonResponse(response)
        except (ValueError, KeyError, DatabaseError) as e:
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
            criteria = dict(search_form.cleaned_data)
            # Prefer structured badge_groups (from formula bar) over legacy tag lists
            parsed_groups = search_form.parse_badge_groups()
            if parsed_groups is not None:
                criteria["badge_groups"] = parsed_groups
            query = Pin.objects.filter(profile=profile).filter_by_criteria(criteria)
            map_data = self.get_map_data(request, query)
            return render(request, "dashboard/pages/map/data.html", {"map_data": map_data})

        logger.error("Invalid search criteria: %s", search_form.errors)
        return HttpResponse(status=400, content="Invalid search criteria.")

    def upload_image(self, request, pin_slug, *args, **kwargs):
        image = request.FILES.get("image")
        pin = Pin.objects.get(slug=pin_slug)
        Image.objects.create(image=image, pin=pin)
        return HttpResponse(status=200)

    def change_category(self, request, pin_slug, *args, **kwargs):
        # TODO: Assess codebase, but this is probably deprecated since the addition of Badges more generically.
        
        category_id = request.POST.get("category")
        pin = Pin.objects.get(slug=pin_slug)
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
        query = Pin.objects.filter(profile=profile).root_pins().select_related("location")

        bbox_str = request.GET.get("bbox", "").strip()
        if bbox_str:
            try:
                parts = [float(x) for x in bbox_str.split(",")]
                if len(parts) == 4:
                    south, west, north, east = parts
                    bbox_poly = Polygon.from_bbox((west, south, east, north))
                    bbox_poly.srid = 4326
                    query = query.filter(point__within=bbox_poly)
            except (ValueError, TypeError) as e:
                logger.warning("Invalid bbox parameter: %s -> %s", bbox_str, e)

        cursor = _safe_positive_int(request.GET.get("cursor"))
        limit = _safe_positive_int(request.GET.get("limit"))
        include_total = request.GET.get("include_total") == "1"
        cached_page = MapPinCache(profile).get_or_build_page(
            query,
            cursor=cursor,
            limit=limit,
            include_total=include_total,
        )
        for pin_dict in cached_page.page.pins:
            pin_dict["viewLocationUrl"] = f"/dashboard/map/pin/{pin_dict['slug']}/"

        payload: dict[str, Any] = {
            "pins": cached_page.page.pins,
            "next_cursor": cached_page.page.next_cursor,
            "cache": "hit" if cached_page.hit else "miss",
        }
        if cached_page.page.total is not None:
            payload["total"] = cached_page.page.total
        return JsonResponse(payload)

    def map_pins_meta(self, request, *args, **kwargs):
        """Return the latest pin update timestamp and app UUID for client-side cache invalidation.

        The client polls this endpoint to detect when the pin collection changed,
        then calls the full pins endpoint only when necessary.  ``app_uuid`` lets
        the client detect a DB wipe or fresh deployment (new UUID → stale cache).

        Returns:
            JsonResponse: ``{"last_updated": "<ISO timestamp>" | null, "app_uuid": "<uuid>"}``
        """
        from django.db.models import Max

        from urbanlens.dashboard.models.site_settings.model import SiteSettings
        profile, _ = Profile.objects.get_or_create(user=request.user)
        result = Pin.objects.filter(profile=profile).root_pins().aggregate(last_updated=Max("updated"))
        last_updated = result["last_updated"]
        site = SiteSettings.get_current()
        return JsonResponse(
            {
                "last_updated": last_updated.isoformat() if last_updated else None,
                "app_uuid": str(site.instance_uuid),
            },
        )

    def map_pin_json(self, request, pin_slug, *args, **kwargs):
        """Return JSON data for a single pin - used for targeted cache updates after edits.

        Args:
            pin_slug: Slug of the pin to return.

        Returns:
            JsonResponse: ``{"pin": {...}}`` or 404 if the pin doesn't belong to the user.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            pin = Pin.objects.filter(profile=profile).select_related("location").get(slug=pin_slug)
        except Pin.DoesNotExist:
            return JsonResponse({"error": "not found"}, status=404)
        map_data = self.get_map_data(request, Pin.objects.filter(pk=pin.pk).select_related("location"))
        if not map_data:
            return JsonResponse({"error": "not found"}, status=404)
        pin_dict = map_data[0]
        pin_dict["viewLocationUrl"] = f"/dashboard/map/pin/{pin.slug}/"
        return JsonResponse({"pin": pin_dict})

    def init_map(self, request, *args, **kwargs):
        map_data = self.get_map_data(request)

        return render(request, "dashboard/pages/map/data.html", {"map_data": map_data})

    def get_map_data(self, request, query: PinQuerySet | None = None):

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if query is None:
            query = Pin.objects.filter(profile=profile).root_pins().select_related("location")

        map_data = MapPinPayloadService(profile).all(query)

        for pin in map_data:
            if "description" in pin and pin["description"] is None:
                pin["description"] = ""

            # Preserve tag objects for popup chips, then collapse to CSV for data.html
            if pin.get("tags"):
                tags = pin["tags"]
                if tags and isinstance(tags[0], dict):
                    pin["tags_data"] = [
                        {"name": t["name"], "color": t.get("color"), "icon": t.get("icon")} for t in tags
                    ]
                    pin["tags"] = ", ".join(t["name"] for t in tags)
                else:
                    pin["tags_data"] = [{"name": t} for t in tags]
                    pin["tags"] = ", ".join(tags)
            else:
                pin["tags_data"] = []
                pin["tags"] = ""
            pin["tags_data_json"] = json.dumps(pin["tags_data"])
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


def _safe_positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed and parsed > 0 else None


def _create_location_with_canonical_name(lat: float, lon: float) -> Location:
    """Create a new Location using its canonical Google place name.

    The user's custom pin name must never be used as a Location name because
    Location.name is shared across all users and visible on the community wiki.
    We ask Google for the real place name and fall back to "Unnamed Location"
    when geocoding is unavailable or returns nothing useful.

    Args:
        lat: Latitude of the new location.
        lon: Longitude of the new location.

    Returns:
        The newly created Location instance.
    """
    from urbanlens.dashboard.services.google.place_info import GooglePlaceService
    from urbanlens.dashboard.services.locations.naming import is_meaningful_name

    google_place = GooglePlaceService().get_or_create_for_coordinates(lat, lon)
    canonical_name = "Unnamed Location"
    if is_meaningful_name(google_place.cached_place_name):
        canonical_name = (google_place.cached_place_name or canonical_name).strip()

    return Location.objects.create(
        name=canonical_name,
        latitude=lat,
        longitude=lon,
        google_place=google_place,
    )


def get_pin_by_address(address: str) -> tuple[float | None, float | None]:
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
