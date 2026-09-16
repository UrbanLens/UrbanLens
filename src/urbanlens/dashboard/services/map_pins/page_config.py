"""Everything the map page's script needs from the server, as one JSON element.

The script itself is a static file, so the browser keeps it between visits; the few dozen values that differ per
request travel beside it instead of being interpolated into it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.middleware.csrf import get_token
from django.urls import reverse

from urbanlens.dashboard.services.core.vendor_assets import vendor_asset_url

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.http import HttpRequest

    from urbanlens.dashboard.models.profile.model import Profile

#: Route arguments the script substitutes per use, so the route is reversed once here.
_PIN_SLUG = "placeholder-slug"
_LIST_UUID = "00000000-0000-0000-0000-000000000000"


def _number(value: Any) -> float | None:
    """*value* as a JSON number, or None when it is not set.

    Args:
        value: A coordinate or radius, possibly a ``Decimal`` or None.

    Returns:
        The value as a float, or None."""
    return None if value is None else float(value)


def _urls() -> dict[str, str]:
    """Every route the map script calls.

    Returns:
        Route names the script knows, mapped to paths."""
    return {
        "labelCreateCategory": reverse("label.create", kwargs={"label_kind": "category"}),
        "labelCreateStatus": reverse("label.create", kwargs={"label_kind": "status"}),
        "labelCreateTag": reverse("label.create", kwargs={"label_kind": "tag"}),
        "listsCreate": reverse("lists.create"),
        "listsItemsAdd": reverse("lists.items.add", args=[_LIST_UUID]),
        "mapAutocompleteEmpty": reverse("map.autocomplete.empty"),
        "mapAutocompleteLocal": reverse("map.autocomplete.local"),
        "mapAutocompletePlaces": reverse("map.autocomplete.places"),
        "mapDocument": reverse("map.document"),
        "mapGeolocationVisits": reverse("map.geolocation.visits"),
        "mapInfrastructure": reverse("map.infrastructure"),
        "mapPinJson": reverse("map.pin.json", kwargs={"pin_slug": _PIN_SLUG}),
        "mapPins": reverse("map.pins"),
        "mapPinsChildren": reverse("map.pins.children"),
        "mapPinsList": reverse("map.pins.list"),
        "mapPinsMeta": reverse("map.pins.meta"),
        "mapPlacesDetails": reverse("map.places.details"),
        "mapPlacesNearby": reverse("map.places.nearby"),
        "mapResolvePlace": reverse("map.resolve_place"),
        "pinAdd": reverse("pin.add"),
        "pinBulkDelete": reverse("pin.bulk_delete"),
        "pinBulkEdit": reverse("pin.bulk_edit"),
        "pinBulkEditLabelOptions": reverse("pin.bulk_edit.label_options"),
        "pinBulkMerge": reverse("pin.bulk_merge"),
        "pinBulkUndo": reverse("pin.bulk_undo"),
        "pinParentSearch": reverse("pin.parent_search"),
        "savedFiltersCounts": reverse("saved_filters.counts"),
        "settingsSaveMapDarkMode": reverse("settings.save_map_dark_mode"),
        "settingsSaveMapPosition": reverse("settings.save_map_position"),
    }


def map_page_config(request: HttpRequest, profile: Profile, context: Mapping[str, Any]) -> dict[str, Any]:
    """The map page's configuration, drawn from the context the page already renders.

    Reading the same context the template does keeps the script's view of the request identical to the page's.

    Args:
        request: The map request, for its CSRF token.
        profile: The viewing profile.
        context: The template context ``view_map`` is about to render.

    Returns:
        A JSON-serializable configuration for the ``#map-page-config`` element."""
    return {
        "urls": _urls(),
        "assets": {
            "leafletMarkerIcon": vendor_asset_url("leaflet_marker_icon"),
            "leafletMarkerShadow": vendor_asset_url("leaflet_marker_shadow"),
        },
        "csrfToken": get_token(request),
        "profileId": profile.pk,
        "profileUuid": str(context["profile_uuid"]),
        "appUuid": str(context["app_uuid"]),
        "openweathermapApiKey": context["openweathermap_api_key"] or "",
        "pinCount": int(context["pin_count"] or 0),
        "clusterRadius": _number(context["cluster_radius"] or None),
        "showOnboardingTips": bool(profile.show_onboarding_tips),
        "showPinCount": bool(context["show_pin_count"]),
        "showFilteredPinCount": bool(context["show_filtered_pin_count"]),
        "showPlacesLayer": bool(context["show_places_layer"]),
        "usePinCache": bool(context["use_pin_cache"]),
        "mapCenterMode": str(context["map_center_mode"]),
        "mapCenterLat": _number(context["map_center_lat"]),
        "mapCenterLng": _number(context["map_center_lng"]),
        "gpsFallbackLat": _number(context["gps_fallback_lat"]),
        "gpsFallbackLng": _number(context["gps_fallback_lng"]),
        "geolocationTrackingAllowed": bool(context["geolocation_tracking_allowed"]),
        "mapDefaultZoom": int(context["map_default_zoom"] or 13),
        "defaultMapView": str(context["default_map_view"] or "street"),
        "mapDarkMode": str(context["map_dark_mode"] or "light"),
    }
