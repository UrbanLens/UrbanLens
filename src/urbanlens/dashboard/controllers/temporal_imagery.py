"""On-demand OpenHistoricalMap (OHM) features for the beta "time slider".

Serves one calendar year's worth of OHM vector features (roads/buildings/
land-use dated as existing that year) for a pin's or wiki's map, so the
frontend slider in ``frontend/ts/shared/temporal-imagery.ts`` can overlay them
on the live Leaflet map. Whether the slider even renders is decided
server-side by ``services.locations.temporal_imagery.temporal_slider_years``
(wired into ``PinController.view()`` and ``LocationWikiView.get()``) - this
view only answers the follow-up fetch once a user has actually dragged it.

This is an explicit stopgap ahead of REData's own future temporal-imagery
endpoints, which do not exist yet - see ``plugins.builtin.satellite_imagery``'s
module docstring for the established precedent of a direct integration like
this being retired once REData ships the equivalent. The Gateway/PanelSource
boundary (``services.apis.locations.open_historical_map`` /
``services.locations.temporal_imagery``) is deliberately kept swappable for
that reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature
from urbanlens.dashboard.services.locations.temporal_imagery import get_temporal_features
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location

#: Stand-in year used to build a per-year URL *template* for the frontend
#: slider, which substitutes the real year client-side. Mirrors
#: ``map_overlays.OVERLAY_UUID_PLACEHOLDER``: Django's ``<int:...>`` path
#: converter will not reverse() against a non-numeric placeholder, so a real
#: (and obviously synthetic) int stands in.
TEMPORAL_YEAR_PLACEHOLDER = 9999


def _resolve_location(request: HttpRequest, pin_slug: str | None, location_slug: str | None) -> Location:
    """Resolve the Location behind a pin-scoped or wiki-scoped temporal-imagery request.

    Same ownership/visibility resolution as
    ``map_overlays._resolve_owner`` - a personal pin only ever resolves for
    its own owner, and a shared Location only through the community
    visibility rules in ``resolve_visible_wiki``.

    Args:
        request: The current request, used for the ownership/visibility checks.
        pin_slug: Slug of a personal pin, on the pin-scoped route.
        location_slug: Slug of a shared Location, on the wiki-scoped route.

    Returns:
        The resolved Location, with coordinates present.

    Raises:
        Http404: Neither slug was supplied, the viewer may not see it, or the
            resolved owner has no Location/coordinates yet.
    """
    if pin_slug is not None:
        location = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user).location
    elif location_slug is not None:
        location = resolve_visible_wiki(request, location_slug)[0]
    else:
        raise Http404
    if location is None or location.latitude is None or location.longitude is None:
        raise Http404
    return location


class TemporalImageryFeaturesView(LoginRequiredMixin, View):
    """GET one year's OpenHistoricalMap features for a pin's or wiki's map.

    ``GET pin/<slug>/temporal/<year>/`` and
    ``GET location/<slug>/wiki/temporal/<year>/`` - beta-gated, matching the
    slider's own visibility rule in ``temporal_slider_years``.
    """

    def get(self, request: HttpRequest, year: int, pin_slug: str | None = None, location_slug: str | None = None) -> JsonResponse:
        """Return the cached-or-freshly-fetched GeoJSON for one historical year.

        Args:
            request: The current request.
            year: The calendar year to fetch features for.
            pin_slug: Slug of the parent pin, on the pin-scoped route.
            location_slug: Slug of the parent location, on the wiki-scoped route.

        Returns:
            ``{"year": ..., "geojson": <FeatureCollection>}``.

        Raises:
            Http404: The viewer lacks ``SiteFeature.BETA_FEATURES``, the owner
                could not be resolved, or ``year`` is out of the plausible range.
        """
        if not user_has_feature(request.user, SiteFeature.BETA_FEATURES):
            raise Http404
        location = _resolve_location(request, pin_slug, location_slug)
        try:
            geojson = get_temporal_features(location, year)
        except ValueError as exc:
            raise Http404 from exc
        return JsonResponse({"year": year, "geojson": geojson})
