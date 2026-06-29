from datetime import datetime
import json
import logging
import operator
from typing import Any
import urllib.parse
import urllib.request

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
from urbanlens.dashboard.models.badges.model import COLOR_CHOICES, ICON_CATEGORIES, Badge
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin import Pin, PinQuerySet
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.map_pins import MapPinCache, MapPinPayloadService
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_US_STATE_CODES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "Washington, D.C.",
}


def _expand_state_codes(states_str: str) -> str:
    """Expand comma-separated US state abbreviations to full state names."""
    if not states_str:
        return ""
    codes = [s.strip().upper() for s in states_str.split(",") if s.strip()]
    return ", ".join(_US_STATE_CODES.get(c, c) for c in codes)


class MapController(LoginRequiredMixin, GenericViewSet):
    def view_map(self, request, *args, **kwargs):
        import json as _json

        from urbanlens.dashboard.models.profile.model import MapCenterMode
        from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

        profile, _ = Profile.objects.get_or_create(user=request.user)
        from urbanlens.dashboard.models.badges.model import KIND_USER

        tags = Badge.objects.tags().visible_to(profile).ordered()
        categories = Badge.objects.categories().ordered()
        filter_badges = Badge.objects.exclude(kind=KIND_USER).visible_to(profile).ordered()
        pin_count = Pin.objects.filter(profile=profile).root_pins().count()

        filter_badges_list = list(filter_badges)
        filter_badges_json = _json.dumps([
            {"id": b.id, "name": b.name, "kind": b.kind, "color": b.color or "", "icon": b.icon or ""}
            for b in filter_badges_list
        ])

        site = SiteSettings.get_current()
        show_pin_count = site.show_dev_admin_features(request.user)
        show_filtered_pin_count = user_has_feature(request.user, SiteFeature.AI)
        show_places_layer = user_has_feature(request.user, SiteFeature.PLACES)

        return render(
            request,
            "dashboard/pages/map/index.html",
            {
                "openweathermap_api_key": settings.openweathermap_api_key,
                "tags": tags,
                "categories": categories,
                "filter_badges": filter_badges_list,
                "filter_badges_json": filter_badges_json,
                "icon_categories": ICON_CATEGORIES,
                "color_choices": COLOR_CHOICES,
                "profile_id": profile.id,
                "profile_uuid": profile.uuid,
                "profile_slug": profile.slug or str(profile.uuid),
                "app_uuid": str(site.instance_uuid),
                "cluster_radius": profile.cluster_radius,
                "pin_count": pin_count,
                "show_pin_count": show_pin_count,
                "show_filtered_pin_count": show_filtered_pin_count,
                "show_places_layer": show_places_layer,
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
            icon = request.POST.get("icon") or None
            color = request.POST.get("color") or None
            custom_icon = request.FILES.get("custom_icon") or None
            badge_ids = request.POST.getlist("badge_ids")
            tag_ids = request.POST.getlist("tag_ids")
            category_ids = request.POST.getlist("category_ids")
            is_private = request.POST.get("is_private") in {"1", "true", "on", "True"}
            google_place_id = request.POST.get("google_place_id") or None

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
                custom_icon=custom_icon,
                color=color,
                is_private=is_private,
                profile=request.user.profile,
            )
            from urbanlens.dashboard.models.badges.model import KIND_USER as _KIND_USER
            if badge_ids:
                pin.badges.set(Badge.objects.exclude(kind=_KIND_USER).filter(id__in=badge_ids))
            else:
                if tag_ids:
                    pin.badges.remove(*pin.badges.filter(kind="tag"))
                    pin.badges.add(*Badge.objects.tags().filter(id__in=tag_ids))
                if category_ids:
                    pin.badges.remove(*pin.badges.filter(kind="category"))
                    pin.badges.add(*Badge.objects.categories().filter(id__in=category_ids))
            pin.save()

            # When adding from a Places layer marker, pre-populate the GooglePlace
            # link on both the pin and its location so subsequent views avoid an
            # extra Places Details API call.
            if google_place_id and not is_private:
                try:
                    from urbanlens.dashboard.services.google.place_info import GooglePlaceService

                    gp_service = GooglePlaceService()
                    gp_service.ensure_linked_by_place_id(pin, google_place_id)
                    if location:
                        gp_service.ensure_linked_by_place_id(location, google_place_id)
                except Exception as gp_exc:
                    logger.warning("Failed to link Google Place %s: %s", google_place_id, gp_exc)

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

    def autocomplete_local(self, request, *args, **kwargs):
        """Fast autocomplete from local DB: pins, locations, aliases, badges, wiki.

        Returns JSON with pin/location suggestions ranked by relevance.  This is
        always the first source shown to the user because it requires no external
        API calls and typically responds within 50-100 ms.
        """
        from urbanlens.dashboard.services.map_pins.autocomplete import search_local

        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": [], "source": "local"})

        profile, _ = Profile.objects.get_or_create(user=request.user)
        results = search_local(q, profile)
        return JsonResponse({"results": [r.to_dict() for r in results], "source": "local"})

    def autocomplete_places(self, request, *args, **kwargs):
        """Proxy Google Places Autocomplete so the API key stays server-side.

        Returns an empty list with ``disabled: true`` when no API key is
        configured so the client can suppress the source gracefully.
        """
        from urbanlens.dashboard.services.map_pins.autocomplete import search_google_places

        q = (request.GET.get("q") or "").strip()
        if len(q) < 2:
            return JsonResponse({"results": [], "source": "places"})

        api_key = settings.google_maps_api_key or settings.google_places_api_key
        if not api_key:
            return JsonResponse({"results": [], "source": "places", "disabled": True})

        results = search_google_places(q, api_key)
        return JsonResponse({"results": [r.to_dict() for r in results], "source": "places"})

    def autocomplete_empty(self, request, *args, **kwargs):
        """Suggestions shown when the search bar is focused but empty.

        Returns top cities by pin count so the user can quickly jump to the
        areas where they have the most pins.
        """
        from urbanlens.dashboard.services.map_pins.autocomplete import empty_suggestions

        profile, _ = Profile.objects.get_or_create(user=request.user)
        results = empty_suggestions(profile)
        return JsonResponse({"results": [r.to_dict() for r in results], "source": "empty"})

    def resolve_place(self, request, *args, **kwargs):
        """Resolve a Google place_id to latitude/longitude coordinates.

        Called when the user selects a Google Places suggestion.  Coordinates
        are intentionally omitted from the autocomplete response to avoid a
        Places Details API call for every suggestion shown.
        """
        from urbanlens.dashboard.services.map_pins.autocomplete import resolve_google_place

        place_id = (request.GET.get("place_id") or "").strip()
        if not place_id:
            return JsonResponse({"error": "missing place_id"}, status=400)

        api_key = settings.google_maps_api_key or settings.google_places_api_key
        if not api_key:
            return JsonResponse({"error": "no_api_key"}, status=503)

        lat, lng, name = resolve_google_place(place_id, api_key)
        if lat is None or lng is None:
            return JsonResponse({"error": "not_found"}, status=404)

        return JsonResponse({"lat": lat, "lng": lng, "name": name or ""})

    def streetview_check(self, request, *args, **kwargs):
        """Check whether Google Street View imagery exists at a given lat/lng.

        Uses the Street View Static API metadata endpoint — a lightweight call
        that returns JSON without downloading any imagery.

        Returns JSON {"available": true/false}.  Falls back to {"available": false}
        on any configuration or network error so the client can degrade gracefully.
        """
        try:
            lat = float(request.GET.get("lat", ""))
            lng = float(request.GET.get("lng", ""))
        except (TypeError, ValueError):
            return JsonResponse({"error": "invalid coordinates"}, status=400)

        api_key = settings.google_street_view_api_key or settings.google_maps_api_key or settings.google_places_api_key
        if not api_key:
            return JsonResponse({"available": False, "reason": "no_key"})

        params = urllib.parse.urlencode({"location": f"{lat},{lng}", "key": api_key, "source": "outdoor"})
        url = f"https://maps.googleapis.com/maps/api/streetview/metadata?{params}"
        try:
            with urllib.request.urlopen(url, timeout=4) as resp:  # nosec B310
                import json as _json
                data = _json.loads(resp.read())
            available = data.get("status") == "OK"
        except Exception:
            available = False

        return JsonResponse({"available": available})

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
            pin_slug: Slug or UUID string of the pin to return.

        Returns:
            JsonResponse: ``{"pin": {...}}`` or 404 if the pin doesn't belong to the user.
        """
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            pin = Pin.objects.filter(profile=profile).select_related("location").get(slug=pin_slug)
        except Pin.DoesNotExist:
            try:
                pin = Pin.objects.filter(profile=profile).select_related("location").get(uuid=pin_slug)
            except (Pin.DoesNotExist, ValueError):
                return JsonResponse({"error": "not found"}, status=404)
        map_data = self.get_map_data(request, Pin.objects.filter(pk=pin.pk).select_related("location"))
        if not map_data:
            return JsonResponse({"error": "not found"}, status=404)
        pin_dict = map_data[0]
        pin_dict["viewLocationUrl"] = f"/dashboard/map/pin/{pin.slug}/"
        return JsonResponse({"pin": pin_dict})

    def patch_pin(self, request, pin_slug, *args, **kwargs):
        """Quick-edit a pin from the map popup dialog.

        Accepts the same FormData fields as ``post_add_pin`` and applies them to
        an existing pin looked up by slug or UUID.  Badges are replaced (not merged)
        when ``badge_ids`` is provided.

        Args:
            pin_slug: Slug or UUID string of the pin to update.

        Returns:
            JsonResponse: ``{"ok": True, "pin_slug": "..."}`` or an error response.
        """
        try:
            try:
                pin = Pin.objects.get(profile=request.user.profile, slug=pin_slug)
            except Pin.DoesNotExist:
                pin = Pin.objects.get(profile=request.user.profile, uuid=pin_slug)
        except (Pin.DoesNotExist, ValueError):
            return JsonResponse({"error": "not found"}, status=404)

        name = request.POST.get("name")
        latitude = request.POST.get("latitude") or None
        longitude = request.POST.get("longitude") or None
        icon = request.POST.get("icon")
        color = request.POST.get("color")
        custom_icon = request.FILES.get("custom_icon") or None
        badge_ids = [bid for bid in request.POST.getlist("badge_ids") if bid]
        is_private = request.POST.get("is_private") in {"1", "true", "on", "True"}

        import contextlib

        if name is not None:
            pin.name = name or None
        if latitude is not None:
            with contextlib.suppress(ValueError):
                pin.latitude = float(latitude)
        if longitude is not None:
            with contextlib.suppress(ValueError):
                pin.longitude = float(longitude)
        if icon is not None:
            pin.icon = icon or None
        if color is not None:
            pin.color = color or None
        if custom_icon:
            pin.custom_icon = custom_icon
        pin.is_private = is_private
        pin.save()

        if badge_ids:
            from urbanlens.dashboard.models.badges.model import KIND_USER as _KIND_USER
            pin.badges.set(Badge.objects.exclude(kind=_KIND_USER).filter(id__in=badge_ids))
        elif "badge_ids" in request.POST:
            pin.badges.clear()

        return JsonResponse({"ok": True, "pin_slug": pin.slug or str(pin.uuid)})

    def nearby_places(self, request, *args, **kwargs):
        """Return Places layer results near a given coordinate, aggregated from enabled sources.

        VIP-only endpoint.  Sources (Google, NPS, Wikipedia) are toggled per user
        profile.  Results are cached per coordinate tile + source set.

        Query params:
            lat: Centre latitude (float).
            lng: Centre longitude (float).
            radius: Search radius in metres for Google Places (default 2000, max 5000).

        Returns:
            JsonResponse: ``{"places": [...], "cached": bool}``
        """
        from django.core.cache import cache as django_cache

        from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

        if not user_has_feature(request.user, SiteFeature.PLACES):
            return JsonResponse({"error": "forbidden"}, status=403)

        try:
            lat = float(request.GET.get("lat", ""))
            lng = float(request.GET.get("lng", ""))
        except (TypeError, ValueError):
            return JsonResponse({"error": "invalid coordinates"}, status=400)

        try:
            radius = min(int(request.GET.get("radius", 2000)), 5000)
        except (TypeError, ValueError):
            radius = 2000

        try:
            zoom = int(request.GET.get("zoom", 10))
        except (TypeError, ValueError):
            zoom = 10

        # Google Places is only useful when zoomed in enough for the radius to be meaningful.
        GOOGLE_MIN_ZOOM = 10

        profile, _ = Profile.objects.get_or_create(user=request.user)
        use_google = profile.places_google_enabled and zoom >= GOOGLE_MIN_ZOOM
        use_nps = profile.places_nps_enabled
        use_wiki = profile.places_wikipedia_enabled

        # Coarse grid key (0.02° ≈ 2 km) so nearby moves reuse the same cached bucket.
        lat_key = round(lat / 0.02) * 0.02
        lng_key = round(lng / 0.02) * 0.02
        source_key = f"{'g' if use_google else ''}{'n' if use_nps else ''}{'w' if use_wiki else ''}"
        django_cache_key = f"ul_places:{lat_key:.2f}:{lng_key:.2f}:{radius}:{source_key}"

        cached = django_cache.get(django_cache_key)
        if cached is not None:
            return JsonResponse({"places": cached, "cached": True})

        site = SiteSettings.get_current()
        cache_seconds = site.google_places_cache_days * 86400
        places: list[dict] = []

        # ── Google historical landmarks (Places API v1 — supports historical_landmark type) ──
        if use_google:
            api_key = settings.google_places_api_key or settings.google_maps_api_key
            if not api_key:
                logger.info("Google Places skipped: no API key configured.")
            else:
                try:
                    from urbanlens.dashboard.services.google.places import GooglePlacesGateway

                    gw = GooglePlacesGateway(api_key=api_key)
                    raw_results = gw.search_nearby(lat, lng, radius=radius, included_types=["historical_landmark"])
                    logger.info("Google Places (new API): found %d results near (%.4f, %.4f)", len(raw_results), lat, lng)
                    for r in raw_results:
                        loc = r.get("location", {})
                        place_lat = loc.get("latitude")
                        place_lng = loc.get("longitude")
                        if place_lat is None or place_lng is None:
                            continue
                        display_name = r.get("displayName", {})
                        name = display_name.get("text", "") if isinstance(display_name, dict) else str(display_name)
                        places.append({
                            "place_id": r.get("id", ""),
                            "name": name,
                            "lat": place_lat,
                            "lng": place_lng,
                            "source": "google",
                            "rating": r.get("rating"),
                            "user_ratings_total": r.get("userRatingCount"),
                            "vicinity": r.get("shortFormattedAddress", ""),
                            "types": r.get("types", []),
                            "icon": "",
                            "description": "",
                            "url": "",
                        })
                except Exception as exc:
                    # TODO: Catch specific exception
                    if "403" in str(exc):
                        logger.warning(
                            "Google Places API returned 403 Forbidden — enable 'Places API (New)' "
                            "in Google Cloud Console and ensure the API key is authorized for places.googleapis.com. "
                            "API key: %s...%s", api_key[:3], api_key[-3:],
                        )
                    else:
                        logger.warning("Google Places nearby search failed: %s", exc)
        elif profile.places_google_enabled:
            logger.debug("Google Places skipped: zoom %d < minimum %d", zoom, GOOGLE_MIN_ZOOM)

        # ── National Park Service ────────────────────────────────────────────
        if use_nps and settings.nps_api_key:
            try:
                from urbanlens.dashboard.services.nps.parks import (
                    NPSGateway,
                    _haversine_km as _nps_haversine,
                    _parse_lat_long as _nps_parse_lat_long,
                )

                nps_cache_key = "ul_nps_all_parks"
                all_parks = django_cache.get(nps_cache_key)
                if all_parks is None:
                    nps_gw = NPSGateway()
                    all_parks = nps_gw.search_parks(limit=500)
                    django_cache.set(nps_cache_key, all_parks, 86400)

                # Filter cached park list by distance without re-hitting the API.
                nearby_parks: list[tuple[float, dict]] = []
                for park in all_parks or []:
                    park_lat, park_lng = _nps_parse_lat_long(park.get("latLong", ""))
                    if park_lat is None or park_lng is None:
                        continue
                    dist = _nps_haversine(lat, lng, park_lat, park_lng)
                    if dist <= 100.0:
                        nearby_parks.append((dist, park))
                nearby_parks.sort(key=operator.itemgetter(0))
                for _dist, park in nearby_parks[:20]:
                    park_lat, park_lng = _nps_parse_lat_long(park.get("latLong", ""))
                    places.append({
                        "place_id": f"nps_{park.get('parkCode', '')}",
                        "name": park.get("fullName", ""),
                        "lat": park_lat,
                        "lng": park_lng,
                        "source": "nps",
                        "description": park.get("description", ""),
                        "url": park.get("url", ""),
                        "types": ["national_park"],
                        "rating": None,
                        "vicinity": _expand_state_codes(park.get("states", "")),
                        "icon": "",
                    })
            except Exception as exc:
                logger.warning("NPS nearby search failed: %s", exc)

        # ── Wikipedia ────────────────────────────────────────────────────────
        if use_wiki:
            try:
                from urbanlens.dashboard.services.wikipedia import WikipediaGateway

                wiki_gw = WikipediaGateway()
                wiki_places = wiki_gw.get_nearby_articles(lat, lng, radius_m=5000, limit=15)
                places.extend(wiki_places)
            except Exception as exc:
                logger.warning("Wikipedia nearby search failed: %s", exc)

        django_cache.set(django_cache_key, places, cache_seconds)
        return JsonResponse({"places": places, "cached": False})

    def place_details(self, request, *args, **kwargs):
        """Return Google Place details for a single place_id.

        VIP-only endpoint.  Fetches editorial summary, formatted address, and
        opening hours.  Results are cached for the same duration as nearby results.

        Query params:
            place_id: Google Place ID.

        Returns:
            JsonResponse: ``{"place": {...}}``
        """
        from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

        if not user_has_feature(request.user, SiteFeature.PLACES):
            return JsonResponse({"error": "forbidden"}, status=403)

        place_id = (request.GET.get("place_id") or "").strip()
        if not place_id:
            return JsonResponse({"error": "missing place_id"}, status=400)

        api_key = settings.google_maps_api_key or settings.google_places_api_key
        if not api_key:
            return JsonResponse({"error": "no_api_key"}, status=503)

        from django.core.cache import cache as django_cache

        django_cache_key = f"ul_place_details_{place_id}"
        cached = django_cache.get(django_cache_key)
        if cached is not None:
            return JsonResponse({"place": cached, "cached": True})

        try:
            from urbanlens.dashboard.services.google.places import GooglePlacesGateway

            gateway = GooglePlacesGateway(api_key=api_key)
            detail = gateway.get_place_details(
                place_id,
                fields=[
                    "name", "formatted_address", "rating", "editorial_summary",
                    "opening_hours", "website", "url", "photos",
                ],
            )
        except Exception as exc:
            logger.warning("Google Place details fetch failed: %s", exc)
            return JsonResponse({"error": "upstream_error"}, status=502)

        site = SiteSettings.get_current()
        cache_seconds = site.google_places_cache_days * 86400
        django_cache.set(django_cache_key, detail, cache_seconds)
        return JsonResponse({"place": detail, "cached": False})

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
