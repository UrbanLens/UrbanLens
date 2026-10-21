"""
Callers treat an environment refusal as "not available here" and move on.

The boundary chain skips the source rather than deferring it; a panel says so rather than caching an empty
answer or vanishing; the name and geocode chains never fall through from a first-party provider to a direct one off
production; messaging, the hosted basemap and the public Overpass mirrors stay production's.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.cache import cache
from django.test import RequestFactory, override_settings
from model_bakery import baker
import requests

from urbanlens.core.tests.provider_fakes import FakePlacesProvider
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.plugins.points import GEOCODERS, PLACES_PROVIDERS
from urbanlens.dashboard.services.apis.locations.geocode_resolution import GeocodeProvider, NominatimGeocoder
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.core.rate_limiter import EnvironmentRefusedError
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain
from urbanlens.dashboard.services.pins.external_data import (
    get_panel_source,
    run_panel_fetch,
    schedule_panel_fetch,
    unavailable_here,
)

_LAT, _LON = 41.73266, -73.92736


@contextlib.contextmanager
def deployment(environment: str, *, overrides: dict[str, float] | None = None) -> Iterator[None]:
    with (
        override_settings(ENVIRONMENT_NAME=environment, TESTING=False),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share", None),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share_overrides", overrides or {}),
    ):
        yield


def _refused(service: str = "overture_maps") -> EnvironmentRefusedError:
    return EnvironmentRefusedError(service, category="quota", environment="development")


def _square() -> MultiPolygon:
    d = 0.0002
    return MultiPolygon(
        Polygon(
            (
                (_LON - d, _LAT - d),
                (_LON + d, _LAT - d),
                (_LON + d, _LAT + d),
                (_LON - d, _LAT + d),
                (_LON - d, _LAT - d),
            )
        ),
        srid=4326,
    )


class _NotHere:
    service_key = "overture_maps"
    boundary_kind = "building"

    def get_typed_boundaries(self, latitude, longitude, *, name=None):
        raise _refused()


class _Footprint:
    service_key = "microsoft_building_footprints"
    boundary_kind = "building"

    def get_typed_boundaries(self, latitude, longitude, *, name=None):
        return {"property": None, "building": _square()}


class BoundaryChainSkipsTests(SimpleTestCase):
    def test_a_refused_source_is_skipped_not_deferred(self) -> None:
        resolved = BoundaryProviderChain(providers=(_NotHere(), _Footprint())).get_boundaries(_LAT, _LON)

        self.assertEqual(resolved.deferred, [])
        self.assertIsNone(resolved.retry_after)
        self.assertIsNotNone(resolved.building_polygon)

    def test_the_public_overture_release_lets_the_refusal_through_unconverted(self) -> None:
        """The public Overture release surfaces the refusal rather than converting it to a rate limit."""
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

        with deployment("development"), self.assertRaises(EnvironmentRefusedError):
            OvertureMapsGateway()._reserve_call_budget("building")


class OverpassMirrorTests(SimpleTestCase):
    def test_development_asks_only_the_configured_primary(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

        gateway = OverpassGateway(base_url="https://primary.example/api/interpreter")
        with deployment("development"):
            self.assertEqual(gateway._endpoints(), [gateway.base_url])
        with deployment("production"):
            self.assertGreater(len(gateway._endpoints()), 1)

    def test_no_primary_and_no_mirrors_is_a_clean_refusal(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import (
            OverpassGateway,
            OverpassUnavailableError,
        )

        gateway = OverpassGateway(base_url="", mirrors=())
        with deployment("development"):
            self.assertEqual(gateway._endpoints(), [])
            with self.assertRaises(OverpassUnavailableError):
                gateway.query("[out:json];node(1);out;")


class OverpassMirrorLedgerTests(TestCase):
    """A failover to a public mirror spends the mirrors' own ``quota`` budget, not the self-hosted primary's."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_failover_is_ledgered_under_the_mirror_service(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries import overpass

        gateway = overpass.OverpassGateway(
            base_url="https://primary.example/api/interpreter",
            mirrors=("https://mirror.example/api/interpreter",),
        )

        def wire(method: str, url: str, *args, **kwargs) -> requests.Response:
            if url == gateway.base_url:
                raise requests.ConnectionError("primary down")
            response = requests.Response()
            response.status_code, response._content, response.url = 200, b'{"elements": []}', url
            return response

        with (
            deployment("production"),
            mock.patch.object(requests.Session, "request", side_effect=wire),
            mock.patch.object(overpass.time, "sleep"),
        ):
            gateway.query("[out:json];node(1);out;")

        self.assertEqual(ApiCallLog.objects.filter(service="overpass").count(), 1)
        self.assertEqual(ApiCallLog.objects.filter(service="overpass_public_mirror").count(), 1)

    def test_staging_holds_the_mirrors_to_its_share_and_the_primary_to_none(self) -> None:
        from urbanlens.dashboard.services.core import rate_limiter

        with deployment("staging"):
            self.assertEqual(rate_limiter._service_share("overpass_public_mirror"), 0.05)
            self.assertEqual(rate_limiter._service_share("overpass"), 1.0)


class PanelUnavailableHereTests(TestCase):
    _SOURCE_KEY = "wikipedia"

    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=_LAT, longitude=_LON))
        self.source = get_panel_source(self._SOURCE_KEY)

    def test_a_refused_fetch_says_so_and_stores_nothing(self) -> None:
        with mock.patch.object(type(self.source), "fetch", side_effect=_refused("wikipedia")):
            run_panel_fetch(self._SOURCE_KEY, self.pin, None)

        self.assertEqual(unavailable_here(self._SOURCE_KEY, self.pin), "wikipedia")
        self.assertFalse(LocationCache.objects.exists())
        self.assertFalse(schedule_panel_fetch(self._SOURCE_KEY, self.pin))

    def test_a_refusal_the_source_swallowed_is_still_seen(self) -> None:
        from urbanlens.dashboard.services.core.egress import require_egress

        def swallow(pin: Pin) -> None:
            with contextlib.suppress(EnvironmentRefusedError):
                require_egress("nominatim")

        with deployment("development"), mock.patch.object(type(self.source), "fetch", side_effect=swallow):
            run_panel_fetch(self._SOURCE_KEY, self.pin, None)

        self.assertEqual(unavailable_here(self._SOURCE_KEY, self.pin), "nominatim")
        self.assertFalse(LocationCache.objects.exists())

    def test_an_ordinary_empty_fetch_is_not_called_unavailable(self) -> None:
        with mock.patch.object(type(self.source), "fetch", return_value=None):
            run_panel_fetch(self._SOURCE_KEY, self.pin, None)

        self.assertIsNone(unavailable_here(self._SOURCE_KEY, self.pin))

    def test_the_panel_renders_the_note_instead_of_vanishing(self) -> None:
        from urbanlens.dashboard.controllers.pin import PinController

        cache.set(self.source.unavailable_key(self.pin), "wikipedia", 60)
        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        response = PinController()._pending_panel(request, self.pin, self._SOURCE_KEY)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Not available in this environment", response.content)
        self.assertNotIn(b"hx-get", response.content)


class SlidesCarouselRefusalTests(TestCase):
    """A carousel shows what the allowed providers found, and trusts that pass only briefly when one was refused."""

    def setUp(self) -> None:
        super().setUp()
        profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=profile, location=baker.make(Location, latitude=_LAT, longitude=_LON))
        self.source = get_panel_source("satellite")

    def _ready_ttl(self, *, refuse: bool) -> int:
        from urbanlens.dashboard.services.core.egress import require_egress
        from urbanlens.dashboard.services.pins import external_data

        def collect(lat: float, lng: float) -> tuple[list, list]:
            if refuse:
                with contextlib.suppress(EnvironmentRefusedError):
                    require_egress("esri")
            return [], []

        with (
            deployment("development"),
            mock.patch.object(type(self.source), "collect", side_effect=collect),
            mock.patch.object(external_data, "cache") as stored,
        ):
            self.source.fetch(self.pin)
        stored.set.assert_called_once()
        return stored.set.call_args.args[2]

    def test_a_refused_provider_keeps_the_pass_short_lived(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import UNAVAILABLE_HERE_TTL_SECONDS

        self.assertEqual(self._ready_ttl(refuse=True), UNAVAILABLE_HERE_TTL_SECONDS)

    def test_a_complete_pass_is_trusted_as_before(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import SLIDES_READY_TTL_SECONDS

        self.assertEqual(self._ready_ttl(refuse=False), SLIDES_READY_TTL_SECONDS)

    def test_a_nested_collector_reports_to_the_outer_one(self) -> None:
        from urbanlens.dashboard.services.core.egress import collect_refusals, require_egress

        with (
            deployment("development"),
            collect_refusals() as outer,
            collect_refusals() as inner,
            contextlib.suppress(EnvironmentRefusedError),
        ):
            require_egress("esri")
        self.assertEqual(inner, ["esri"])
        self.assertEqual(outer, ["esri"])


class NameChainTests(SimpleTestCase):
    """A first-party places provider failing off production is "no name for now", never a call to direct Google Geocoding."""

    def setUp(self) -> None:
        super().setUp()
        no_geocoders = GEOCODERS.overridden([])
        no_geocoders.__enter__()
        self.addCleanup(no_geocoders.__exit__, None, None, None)

    def _run(self, environment: str) -> tuple[str | None, mock.Mock]:
        from urbanlens.dashboard.services.locations.google import (
            GoogleGeocodingNameResolver,
            GooglePlacesNameResolver,
            PlaceNameResolverChain,
        )

        with (
            deployment(environment),
            PLACES_PROVIDERS.overridden([FakePlacesProvider(shields_direct=True)]),
            mock.patch("urbanlens.dashboard.services.locations.google.GoogleGeocodingGateway") as direct,
        ):
            direct.return_value.get_place_name.return_value = "Hudson River State Hospital"
            chain = PlaceNameResolverChain(resolvers=(GooglePlacesNameResolver(), GoogleGeocodingNameResolver()))
            name = chain.resolve(_LAT, _LON)
        return name, direct

    def test_staging_does_not_fall_through_to_google(self) -> None:
        name, direct = self._run("staging")
        self.assertIsNone(name)
        direct.assert_not_called()

    def test_production_still_does(self) -> None:
        name, _ = self._run("production")
        self.assertEqual(name, "Hudson River State Hospital")


class _ShieldingGeocoder(GeocodeProvider):
    """A first-party geocoder that is always unavailable."""

    key = "shielding"
    shields_direct = True

    def available(self) -> bool:
        return True

    def geocode(self, address: str) -> tuple[float | None, float | None]:
        raise ProviderUnavailableError("budget exhausted")


class GeocodeChainTests(SimpleTestCase):
    _MODULE = "urbanlens.dashboard.services.apis.locations.geocode_resolution"

    def _run(self, environment: str):
        with (
            deployment(environment),
            GEOCODERS.overridden([_ShieldingGeocoder(), NominatimGeocoder()]),
            mock.patch(f"{self._MODULE}.nominatim_geocode", return_value=(1.0, 2.0)) as nominatim,
        ):
            from urbanlens.dashboard.services.apis.locations.geocode_resolution import geocode_address

            try:
                return geocode_address("1 Main St"), nominatim
            except ProviderUnavailableError as exc:
                return exc, nominatim

    def test_a_shielding_providers_unavailability_surfaces_off_production(self) -> None:
        for environment in ("staging", "development"):
            with self.subTest(environment=environment):
                result, nominatim = self._run(environment)
                self.assertIsInstance(result, ProviderUnavailableError)
                nominatim.assert_not_called()

    def test_production_falls_back(self) -> None:
        result, nominatim = self._run("production")
        self.assertEqual(result, (1.0, 2.0))
        nominatim.assert_called_once()

    def test_pin_creation_reports_unavailable_not_no_such_place(self) -> None:
        from urbanlens.dashboard.services.pins.pin_creation import (
            AddressLookupUnavailableError,
            coordinates_for_address,
        )

        with (
            mock.patch(
                "urbanlens.dashboard.services.pins.pin_creation.get_pin_by_address",
                side_effect=ProviderUnavailableError("x"),
            ),
            self.assertRaises(AddressLookupUnavailableError),
        ):
            coordinates_for_address("1 Main St")

    def test_nominatim_propagates_the_refusal_rather_than_reporting_no_results(self) -> None:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        with deployment("development"), self.assertRaises(EnvironmentRefusedError):
            NominatimGateway().search("1 Main St", limit=1)


class MessagingTests(TestCase):
    def test_a_text_off_production_is_a_no_op(self) -> None:
        from urbanlens.dashboard.services.apis.messaging.sms import SmsGateway

        gateway = SmsGateway(account_sid="AC1", auth_token="t", from_number="+15550000000")
        with deployment("staging"), mock.patch("requests.Session.request") as wire:
            self.assertFalse(gateway.send("+15551234567", "hello"))
        wire.assert_not_called()
        self.assertFalse(ApiCallLog.objects.filter(service="sms").exists())

    def test_an_override_opts_texts_in(self) -> None:
        from urbanlens.dashboard.services.apis.messaging.sms import SmsGateway

        gateway = SmsGateway(account_sid="AC1", auth_token="t", from_number="+15550000000")
        with deployment("staging", overrides={"sms": 1.0}), mock.patch("requests.Session.request") as wire:
            wire.return_value = mock.Mock(ok=True, status_code=201, raise_for_status=mock.Mock())
            self.assertTrue(gateway.send("+15551234567", "hello"))
        wire.assert_called_once()

    def test_push_off_production_records_nothing_against_the_devices(self) -> None:
        from urbanlens.dashboard.services.notifications import push

        with (
            deployment("staging"),
            mock.patch.object(push, "_post_unifiedpush") as post,
            mock.patch.object(push, "_record_delivery") as record,
        ):
            self.assertEqual(push.send_push_to_devices([1, 2], {"title": "x"}), 0)
        post.assert_not_called()
        record.assert_not_called()


class WaybackSaveTests(SimpleTestCase):
    def test_development_reads_nothing_and_writes_nothing(self) -> None:
        from urbanlens.dashboard.services.apis.locations.wayback_machine import WaybackMachineGateway

        with (
            deployment("development"),
            mock.patch("requests.Session.request") as wire,
            self.assertRaises(EnvironmentRefusedError),
        ):
            WaybackMachineGateway().save_url("https://example.com/")
        wire.assert_not_called()

    def test_staging_may_read_but_never_saves(self) -> None:
        from urbanlens.dashboard.services.core.egress import egress_permitted

        with deployment("staging"):
            self.assertTrue(egress_permitted("wayback_machine"))
            self.assertFalse(egress_permitted("wayback_save"))


class HostedBasemapTests(SimpleTestCase):
    def test_the_key_is_handed_out_on_production_and_staging_only(self) -> None:
        from urbanlens.dashboard.services.core.egress import hosted_basemap_api_key
        from urbanlens.dashboard.services.map.basemap_catalogue import hosted_basemap_tiles

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.protomaps_api_key", "key"):
            for environment in ("production", "staging"):
                with self.subTest(environment=environment), deployment(environment):
                    self.assertEqual(hosted_basemap_api_key(), "key")
                    self.assertIn("key=key", hosted_basemap_tiles() or "")
            for environment in ("development", "local"):
                with self.subTest(environment=environment), deployment(environment):
                    self.assertEqual(hosted_basemap_api_key(), "")
                    self.assertIsNone(hosted_basemap_tiles())


class BackgroundTaskGateTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.core import egress

        egress._last_logged.clear()

    def test_an_external_sweep_enqueued_off_production_does_nothing(self) -> None:
        from urbanlens.dashboard.tasks import sweep_unarchived_links

        with deployment("staging"), mock.patch("urbanlens.dashboard.services.links.wayback_archive.sweep") as sweep:
            self.assertIsNone(sweep_unarchived_links())
        sweep.assert_not_called()

    def test_an_allow_listed_one_runs(self) -> None:
        from urbanlens.dashboard.tasks import sweep_unarchived_links

        with (
            deployment("staging"),
            mock.patch(
                "urbanlens.UrbanLens.settings.app.settings.background_tasks_allowlist", ["wayback-archive-sweep"]
            ),
            mock.patch("urbanlens.dashboard.services.links.wayback_archive.sweep", return_value={}) as sweep,
        ):
            sweep_unarchived_links()
        sweep.assert_called_once()

    def test_only_an_external_entry_can_be_gated(self) -> None:
        from urbanlens.dashboard.services.core.egress import GATED_BACKGROUND_TASKS, external_background_task

        entry = "not-a-beat-entry-for-this-test"
        self.addCleanup(GATED_BACKGROUND_TASKS.pop, entry, None)

        @external_background_task(entry)
        def sweep() -> None:
            """Stands in for a task that is not classified as reaching outside."""

        with self.assertRaises(ValueError):
            sweep()


class OperatorPathsTests(SimpleTestCase):
    """Paths outside the gateway session that ask the policy themselves."""

    def test_the_places_diagnostic_refuses_off_production(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with (
            deployment("development"),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "k"),
            mock.patch.object(requests.Session, "request") as wire,
        ):
            call_command("diagnose_places_api", stdout=out)
        wire.assert_not_called()
        self.assertIn("does not call google_places", out.getvalue())

    def test_a_wikipedia_cover_is_not_downloaded_off_production(self) -> None:
        from urbanlens.dashboard.services.wiki import wiki_seed

        with deployment("development"), mock.patch.object(wiki_seed, "request_public_url") as fetch:
            wiki_seed._store_cover_from_url("https://upload.wikimedia.org/a.jpg", pin=mock.Mock(), wiki=None)
        fetch.assert_not_called()
