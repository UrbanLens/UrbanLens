"""UrbanLens's own pipeline turns one pin on a notable campus into the whole property, against a real REData.

One test per site: create a root pin the way the map does, run the bootstrap chain it sets off inline, then check
the structure a person opening the pin would expect - the parcel on the top pin and its wiki, a child pin and child
wiki per building, each building outlined, build dates, and the site's content cached. Needs a test database
(``bin/host_pytest.sh`` provides one) and spends real budget on REData and the public sources behind it.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from live_sites import LiveRedata, Site
import pytest
import requests

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.django_db(transaction=True)

#: Panels whose answer every campus should have once the bootstrap finishes; the rest are judged per site.
_SITE_PANELS = ("property_records", "redata_historic_registers", "searxng_images")

#: Share of building child pins whose place must carry a footprint polygon, as for REData's own answer.
_OUTLINED_SHARE = 0.8


@contextmanager
def _live_redata(redata: LiveRedata) -> Iterator[None]:
    """Point UrbanLens's settings at the live REData, and send its Host header on every request to it."""
    from urbanlens.UrbanLens.settings.app import settings as app_settings

    origin = redata.base_url.removesuffix("api/v1/").rstrip("/")
    host = redata.session.headers.get("Host", "")
    original = requests.Session.request

    def request(session: requests.Session, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        if host and str(url).startswith(origin):
            kwargs["headers"] = {**(kwargs.get("headers") or {}), "Host": host, "X-Forwarded-Proto": "https"}
        return original(session, method, url, *args, **kwargs)

    api_key = str(redata.session.headers["Authorization"]).removeprefix("Bearer ")
    with (
        mock.patch.object(app_settings, "redata_api_url", origin),
        mock.patch.object(app_settings, "redata_api_key", api_key),
        mock.patch.object(requests.Session, "request", request),
    ):
        yield


@pytest.fixture
def bootstrapped(redata: LiveRedata, site: Site, django_capture_on_commit_callbacks: Callable[..., Any]) -> Any:
    """The root pin a person drops on the campus, after everything its creation sets off has run."""
    from urbanlens.core.tests.celery_inline import tasks_run_inline
    from urbanlens.dashboard import tasks
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

    User.objects.create_user(username="live-first")  # absorbs the first-user site-admin promotion
    profile = User.objects.create_user(username=f"live-{site.key}").profile
    inline = (
        tasks.bootstrap_location,
        tasks.fetch_panel_source,
        tasks.ensure_wiki_for_location,
        tasks.ensure_wikis_for_locations,
        tasks.ensure_building_wikis,
    )
    with _live_redata(redata), tasks_run_inline(*inline), django_capture_on_commit_callbacks(execute=True):
        pin = create_pin_for_profile(profile, name=site.name, latitude=site.lat, longitude=site.lng).pin
    return Pin.objects.select_related("location__place", "wiki").get(pk=pin.pk)


@pytest.mark.live_check("pipeline")
def test_one_pin_becomes_the_whole_property(site: Site, bootstrapped: Any, subtests: Any) -> None:
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.place.model import PlaceKind
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, get_panel_source
    from urbanlens.dashboard.services.pins.pin_restructure import enclosing_parcel

    root = bootstrapped
    point = Point(site.lng, site.lat, srid=4326)
    parcel = enclosing_parcel(root.location.place) if root.location.place else None
    wiki = Wiki.objects.filter(location=root.location).first()

    with subtests.test("the top parent stands on the parcel holding the campus point"):
        assert parcel is not None and parcel.kind == PlaceKind.PARCEL, f"{site.name}: no parcel place under the pin"
        assert parcel.geometry is not None and parcel.geometry.contains(point)

    with subtests.test("the top parent's wiki is on the same parcel"):
        assert wiki is not None, f"{site.name}: no wiki"
        assert parcel is not None and wiki.place_id == parcel.pk

    if site.standing:
        children = list(root.descendants().select_related("location__place"))
        with subtests.test("every building has a child pin"):
            assert len(children) >= site.required_buildings, (
                f"{site.name}: {len(children)} building pins, want {site.required_buildings}"
            )
        with subtests.test("the building pins are outlined"):
            outlined = [
                child
                for child in children
                if child.location.place
                and child.location.place.geometry
                and child.location.place.geometry.geom_type in {"Polygon", "MultiPolygon"}
            ]
            assert children and len(outlined) / len(children) >= _OUTLINED_SHARE, (
                f"{site.name}: {len(outlined)} of {len(children)} building pins outlined"
            )
        with _subcheck(subtests, site, "building_wikis", "each building pin resolves to a wiki under the campus wiki"):
            # Pins on parts of one building share its wiki, so the count of wikis is not the measure.
            resolved = [child for child in children if wiki is not None and _building_wiki(child, wiki) is not None]
            assert len(resolved) >= len(children) * _OUTLINED_SHARE, (
                f"{site.name}: {len(resolved)} of {len(children)} building pins resolve to a building wiki"
            )
        with _subcheck(subtests, site, "build_date", "the campus carries its build date"):
            assert root.date_built is not None, f"{site.name}: no date_built on the root pin"

    for key in _SITE_PANELS:
        with subtests.test(f"{key} is cached for the campus"):
            panel = get_panel_source(key)
            assert isinstance(panel, LocationCachePanelSource), f"{key} is not a cached panel source"
            rows = LocationCache.objects.filter(
                location__in=_campus_locations(root), source=panel.cache_source
            ).exclude(data={})
            assert rows.exists(), f"{site.name}: no {key} answer cached"


@contextmanager
def _subcheck(subtests: Any, site: Site, name: str, title: str) -> Iterator[None]:
    """One subtest, reported as skipped when ``kirkbrides.toml`` names it a known issue and it still fails.

    A known issue that passes fails instead, so a fix cannot hide behind its entry.
    """
    issue = site.known_issues.get(f"pipeline.{name}")
    with subtests.test(title):
        if issue is None:
            yield
            return
        try:
            yield
        except AssertionError:
            pytest.skip(f"{site.key} pipeline.{name}: known issue, {issue}")
        else:
            pytest.fail(f"{site.key} pipeline.{name} passes now; remove its known_issues entry ({issue})")


def _building_wiki(child: Any, campus_wiki: Any) -> Any:
    """The wiki a building pin's location reads as its own: its location's, its building place's, or the one
    the building it stands on already has."""
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.building_wikis import standing_building

    location = child.location
    own = Wiki.objects.filter(location=location).first() or (
        Wiki.objects.filter(place=location.place).first() if location.place_id else None
    )
    if own is not None:
        return own
    standing = standing_building(location, campus_wiki)
    return standing.wiki if standing is not None else None


def _campus_locations(root: Any) -> list[int]:
    return [root.location_id, *root.descendants().values_list("location_id", flat=True)]
