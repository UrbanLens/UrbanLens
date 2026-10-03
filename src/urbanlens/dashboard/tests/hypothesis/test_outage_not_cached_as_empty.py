"""An outage must not be cached as "there is nothing here"."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin


class SearxngImageOutageTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        location = baker.make(
            Location,
            latitude=41.73,
            longitude=-73.92,
            official_name="Hudson River State Hospital",
            official_name_source="google_places",
        )
        self.pin = baker.make(
            Pin, profile=self.profile, location=location, parent_pin=None, name="Hudson River State Hospital"
        )

    def _source(self):
        from urbanlens.dashboard.plugins.builtin.searxng_images import SearxngImageMediaSource

        return SearxngImageMediaSource()

    def _cached(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=self._source().cache_source).count()

    def test_an_outage_leaves_the_source_unfetched(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web",
            side_effect=LocationContextUnavailableError("source_error", "503"),
        ):
            self._source().fetch(self.pin)

        self.assertEqual(
            self._cached(), 0, "caching the outage makes it permanent - nothing refetches a source that has a row"
        )

    def test_a_genuine_empty_result_is_cached(self) -> None:
        """Asked and told nothing is a real answer, and must not be refetched forever."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web",
            return_value=[],
        ):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 1)

    def test_results_are_cached(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web",
            return_value=[{"url": "https://example.test/a.jpg"}],
        ):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 1)


class SiteConditionsOutageTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.74, longitude=-73.93)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)

    def _source(self):
        from urbanlens.dashboard.plugins.builtin.redata_site_conditions import SiteConditionsPanelSource

        return SiteConditionsPanelSource()

    def _cached(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=self._source().cache_source).count()

    def test_a_total_outage_leaves_it_unfetched(self) -> None:
        targets = [
            "urbanlens.dashboard.services.apis.locations.redata_land_cover_gateway.RedataLandCoverGateway.get_land_cover",
            "urbanlens.dashboard.services.apis.locations.redata_walkability_gateway.RedataWalkabilityGateway.get_walkability",
            "urbanlens.dashboard.services.apis.locations.redata_soil_gateway.RedataSoilGateway.get_soil_components",
        ]
        with (
            mock.patch(targets[0], side_effect=LocationContextUnavailableError("source_error", "down")),
            mock.patch(targets[1], side_effect=LocationContextUnavailableError("source_error", "down")),
            mock.patch(targets[2], side_effect=LocationContextUnavailableError("source_error", "down")),
        ):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 0)


class RedataPartialProviderOutageTests(TestCase):
    """A provider outage *inside* a successful request is still an outage.

    The tests above guard a failed request."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        location = baker.make(
            Location,
            latitude=41.73,
            longitude=-73.92,
            official_name="Hudson River State Hospital",
            official_name_source="google_places",
        )
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name="HRSH")

    def _source(self):
        from urbanlens.dashboard.plugins.builtin.redata_permits import BuildingPermitsPanelSource

        return BuildingPermitsPanelSource()

    def _cached(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=self._source().cache_source).count()

    def _envelope(self, *, complete: bool, results: list[dict]):
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

        return LocationContextEnvelope(count=len(results), complete=complete, results=results, providers=[])

    def test_an_empty_incomplete_answer_is_not_cached(self) -> None:
        source = self._source()
        with mock.patch.object(type(source), "fetch_envelope", return_value=self._envelope(complete=False, results=[])):
            source.fetch(self.pin)

        self.assertEqual(self._cached(), 0, "could not ask is not an answer - a row here makes the blank permanent")

    def test_an_empty_complete_answer_is_cached(self) -> None:
        """Asked and told nothing is a real result, and must not be refetched forever."""
        source = self._source()
        with mock.patch.object(type(source), "fetch_envelope", return_value=self._envelope(complete=True, results=[])):
            source.fetch(self.pin)

        self.assertEqual(self._cached(), 1)

    def test_a_partial_answer_with_rows_is_cached(self) -> None:
        """One flaky provider must not stop the other four's rows being stored."""
        source = self._source()
        with mock.patch.object(
            type(source),
            "fetch_envelope",
            return_value=self._envelope(complete=False, results=[{"permit_number": "A-1"}]),
        ):
            source.fetch(self.pin)

        self.assertEqual(self._cached(), 1)

    def test_the_rule_is_inherited_by_every_panel_built_on_the_base(self) -> None:
        """Stated as a property of the base rather than of one panel.

        Six panels share this fetch; asserting it on one of them only proves
        the base works, which is the point of moving the rule there.
        """
        from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

        for source_cls in RedataInfoPanelSource.__subclasses__():
            with self.subTest(panel=source_cls.__name__):
                self.assertIs(
                    source_cls.fetch,
                    RedataInfoPanelSource.fetch,
                    f"{source_cls.__name__} overrides fetch and so opts out of the outage rule",
                )
                self.assertTrue(
                    getattr(source_cls, "payload_key", ""),
                    f"{source_cls.__name__} declares no payload_key, so the inherited fetch has nowhere to write",
                )


class HistoricalMapMediaOutageTests(TestCase):
    _LOOKUP = (
        "urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway."
        "RedataHistoricalMapsGateway.get_maps_covering"
    )

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        location = baker.make(
            Location,
            latitude=41.73,
            longitude=-73.92,
            official_name="Hudson River State Hospital",
            official_name_source="google_places",
        )
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name="HRSH")

    def _source(self):
        from urbanlens.dashboard.plugins.builtin.redata_historical_map_media import HistoricalMapMediaSource

        return HistoricalMapMediaSource()

    def _cached(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=self._source().cache_source).count()

    def test_an_outage_leaves_the_source_unfetched(self) -> None:
        with mock.patch(self._LOOKUP, side_effect=LocationContextUnavailableError("source_error", "503")):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 0)

    def test_a_genuine_empty_result_is_cached(self) -> None:
        with mock.patch(self._LOOKUP, return_value=[]):
            self._source().fetch(self.pin)

        self.assertEqual(self._cached(), 1)


_PROPERTY_GATEWAY = "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway"


def _hrsh(**fields) -> Location:
    return baker.make(
        Location,
        latitude=41.7321,
        longitude=-73.9262,
        official_name="Hudson River State Hospital",
        official_name_source="google_places",
        **fields,
    )


class CrisEnrichmentOutageTests(RedataConfiguredMixin, TestCase):
    """The batch's CRIS fetch, which wrote 50 empty rows in the production outage."""

    def setUp(self) -> None:
        super().setUp()
        self.location = _hrsh()

    def _enrich(self, **lookup) -> None:
        from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingEnrichmentSource

        with mock.patch(f"{_PROPERTY_GATEWAY}.lookup_cultural_resources", **lookup):
            CrisBuildingEnrichmentSource().enrich(self.location)

    def _rows(self) -> int:
        return LocationCache.objects.filter(location=self.location, source="cris_building_usn").count()

    def test_an_outage_writes_nothing(self) -> None:
        with self.assertRaises(PropertyRecordsUnavailableError):
            self._enrich(side_effect=PropertyRecordsUnavailableError("source_error", "Could not reach REData"))

        self.assertEqual(self._rows(), 0)

    def test_a_settled_refusal_is_cached_as_empty(self) -> None:
        self._enrich(side_effect=PropertyRecordsUnavailableError("no_data_found", ""))

        self.assertEqual(self._rows(), 1)

    def test_a_genuine_empty_answer_is_cached(self) -> None:
        self._enrich(return_value=[])

        self.assertEqual(self._rows(), 1)


class CrisDetailOutageTests(RedataConfiguredMixin, TestCase):
    """A building found but its detail unreachable: the row would claim attachments it never saw."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        location = _hrsh()
        self.pin = baker.make(Pin, profile=baker.make(User).profile, location=location, parent_pin=None)
        self.building = {
            "uuid": "b-1",
            "resource_type": "building",
            "provider": "ny_cris",
            "source_latitude": 41.7321,
            "source_longitude": -73.9262,
            "attributes": {"USNName": "Main Building"},
        }

    def _fetch(self, **detail) -> None:
        from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource

        with (
            mock.patch(f"{_PROPERTY_GATEWAY}.lookup_cultural_resources", return_value=[self.building]),
            mock.patch(f"{_PROPERTY_GATEWAY}.fetch_cultural_resource_detail", **detail),
            mock.patch("urbanlens.dashboard.services.locations.site_scope.is_site_scope", return_value=False),
        ):
            CrisBuildingPanelSource()._fetch_now(self.pin)

    def _rows(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source="cris_building_usn").count()

    def test_an_unreachable_detail_writes_nothing(self) -> None:
        with self.assertRaises(PropertyRecordsUnavailableError):
            self._fetch(side_effect=PropertyRecordsUnavailableError("source_error", "503"))

        self.assertEqual(self._rows(), 0)

    def test_a_detail_redata_does_not_have_keeps_the_search_row(self) -> None:
        self._fetch(side_effect=PropertyRecordsUnavailableError("no_data_found", ""))

        self.assertEqual(self._rows(), 1)


class PlaceDetailsEnrichmentOutageTests(RedataConfiguredMixin, TestCase):
    """The batch's place-details fetch, which wrote 50 empty rows in the production outage."""

    _DETAIL = "urbanlens.dashboard.services.apis.locations.google.redata_cid_gateway.RedataCidGateway.get_place_detail"

    def setUp(self) -> None:
        super().setUp()
        google_place = GooglePlace.objects.create(latitude="41.7321", longitude="-73.9262", cid=123456789012345678)
        self.location = _hrsh(google_place=google_place)

    def _rows(self) -> int:
        return LocationCache.objects.filter(location=self.location, source="redata_place_details").count()

    def _enrich(self, **detail) -> None:
        from urbanlens.dashboard.plugins.builtin.redata_place_details import RedataPlaceDetailsEnrichmentSource

        with mock.patch(self._DETAIL, **detail):
            RedataPlaceDetailsEnrichmentSource().enrich(self.location)

    def test_an_outage_writes_nothing(self) -> None:
        with self.assertRaises(GatewayRequestError):
            self._enrich(side_effect=GatewayRequestError("Could not reach REData: ConnectionError"))

        self.assertEqual(self._rows(), 0)

    def test_a_cid_redata_never_resolved_is_cached_as_empty(self) -> None:
        self._enrich(return_value=None)

        self.assertEqual(self._rows(), 1)


class ParcelBuildingsOutageTests(RedataConfiguredMixin, TestCase):
    """REData's building list, whose outage left 48 empty rows and so blanked the building attributes read from them."""

    def setUp(self) -> None:
        super().setUp()
        self.location = _hrsh()

    def _rows(self, source: str = "parcel_buildings") -> int:
        return LocationCache.objects.filter(location=self.location, source=source).count()

    def _enrich(self, **parcel) -> None:
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsEnrichmentSource

        with mock.patch(f"{_PROPERTY_GATEWAY}.lookup_parcel_uuid", **parcel):
            ParcelBuildingsEnrichmentSource().enrich(self.location)

    def test_an_outage_writes_nothing(self) -> None:
        with self.assertRaises(PropertyRecordsUnavailableError):
            self._enrich(side_effect=PropertyRecordsUnavailableError("source_error", "Could not reach REData"))

        self.assertEqual(self._rows(), 0)

    def test_no_parcel_is_cached_as_empty(self) -> None:
        self._enrich(side_effect=PropertyRecordsUnavailableError("no_data_found", ""))

        self.assertEqual(self._rows(), 1)

    def test_building_attributes_after_a_parcel_outage_write_nothing(self) -> None:
        """The production cascade: the attributes source answers from the cached building list when there is one."""
        from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsEnrichmentSource
        from urbanlens.dashboard.plugins.builtin.redata_building_attributes import (
            RedataBuildingAttributesEnrichmentSource,
        )

        outage = PropertyRecordsUnavailableError("source_error", "Could not reach REData")
        with mock.patch(f"{_PROPERTY_GATEWAY}.lookup_parcel_uuid", side_effect=outage):
            for source in (ParcelBuildingsEnrichmentSource(), RedataBuildingAttributesEnrichmentSource()):
                with self.assertRaises(PropertyRecordsUnavailableError):
                    source.enrich(self.location)

        self.assertEqual(self._rows() + self._rows("redata_building_attributes"), 0)


class MediaArchiveOutageTests(TestCase):
    """Smithsonian, LOC, Internet Archive and the rest share ``MediaProvider.get_media``, for base and audience rows."""

    _SEARCH = (
        "urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway."
        "RedataReferenceDocumentsGateway.search"
    )

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        location = _hrsh(locality="Poughkeepsie", administrative_area_level_1="NY", country="US")
        # A name of the owner's own, so the pin reads an audience row beside the shared one.
        self.pin = baker.make(
            Pin, profile=baker.make(User).profile, location=location, parent_pin=None, name="Hudson River Psychiatric"
        )

    def _source(self):
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import (
            SmithsonianMediaProvider,
        )
        from urbanlens.dashboard.services.pins.external_data import MediaPanelSource

        return MediaPanelSource("smithsonian", SmithsonianMediaProvider.service_key, SmithsonianMediaProvider)

    def _audiences(self) -> set[str]:
        rows = LocationCache.objects.filter(location=self.pin.location, source="smithsonian")
        return set(rows.values_list("audience", flat=True))

    def test_the_fixture_reads_two_rows(self) -> None:
        self.assertEqual(len(self._source().search_scopes(self.pin)), 2)

    def _fetch_during_an_outage(self) -> None:
        with (
            mock.patch(self._SEARCH, side_effect=LocationContextUnavailableError("source_error", "503")),
            self.assertRaises(LocationContextUnavailableError),
        ):
            self._source().fetch(self.pin)

    def test_an_outage_writes_no_shared_row(self) -> None:
        self._fetch_during_an_outage()

        self.assertEqual(self._audiences(), set())

    def test_an_outage_writes_no_row_for_the_owners_names(self) -> None:
        LocationCache.set(self.pin.location, "smithsonian", {"items": []})

        self._fetch_during_an_outage()

        self.assertEqual(self._audiences(), {""})

    def test_an_outage_leaves_a_stale_good_row_as_it_was(self) -> None:
        stale = LocationCache.set(self.pin.location, "smithsonian", {"items": [{"url": "https://example.test/a"}]})
        LocationCache.objects.filter(pk=stale.pk).update(updated=stale.updated.replace(year=2020))
        stale.refresh_from_db()

        self._fetch_during_an_outage()

        after = LocationCache.objects.get(pk=stale.pk)
        self.assertEqual((after.data, after.updated), (stale.data, stale.updated))

    def test_a_genuine_empty_answer_is_cached_for_both(self) -> None:
        with mock.patch(self._SEARCH, return_value=[]):
            self._source().fetch(self.pin)

        self.assertEqual(len(self._audiences()), 2)


class MediaProviderPartialOutageTests(TestCase):
    """One of a provider's queries failing does not stop the others' results being stored."""

    def _provider(self, outcomes: dict[str, Exception | list[str]]):
        from dataclasses import dataclass

        from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider

        @dataclass(slots=True, kw_only=True)
        class Provider(MediaProvider):
            service_key = "outage_probe"

            def _generate_media(self, search_term: str, address: str | None = None):
                outcome = outcomes[search_term]
                if isinstance(outcome, Exception):
                    raise outcome
                yield from (MediaItem(url=url, thumb_url=url, caption="", source="probe") for url in outcome)

        return Provider()

    def _rows(self, location: Location) -> int:
        return LocationCache.objects.filter(location=location, source="outage_probe").count()

    def test_an_outage_on_one_query_and_results_on_another_are_cached(self) -> None:
        location = _hrsh()
        provider = self._provider({"a": requests.ConnectionError("refused"), "b": ["https://example.test/1"]})

        items, _from_cache = provider.get_media(location, ["a", "b"])

        self.assertEqual(len(items), 1)
        self.assertEqual(self._rows(location), 1)

    def test_an_outage_on_one_query_and_nothing_on_another_is_not_cached(self) -> None:
        location = _hrsh()
        provider = self._provider({"a": requests.ConnectionError("refused"), "b": []})

        with self.assertRaises(requests.ConnectionError):
            provider.get_media(location, ["a", "b"])

        self.assertEqual(self._rows(location), 0)

    def test_a_failure_that_is_not_an_outage_is_still_an_answer(self) -> None:
        location = _hrsh()
        provider = self._provider({"a": ValueError("unparseable")})

        provider.get_media(location, ["a"])

        self.assertEqual(self._rows(location), 1)


class WebSearchOutageTests(RedataConfiguredMixin, TestCase):
    """The page's own fetch shows "try later" and stores nothing; the warming task stores nothing either."""

    _SEARCH = "urbanlens.dashboard.services.apis.locations.redata_search_gateway.RedataSearchGateway.search_web"

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        grant_subscription(self.user, baker.make(SubscriptionRole, features=SiteFeature.SEARCH), self.user, None)
        location = _hrsh(locality="Poughkeepsie", administrative_area_level_1="NY", country="US")
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None, name="Hudson River")

    def _rows(self) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source="web_search").count()

    def _view(self):
        from django.test import RequestFactory

        from urbanlens.dashboard.controllers.pin import PinController

        request = RequestFactory().get("/")
        request.user = self.user
        return PinController().web_search(request, pin_slug=self.pin.slug)

    def test_the_page_says_try_later_and_stores_nothing(self) -> None:
        with mock.patch(self._SEARCH, side_effect=LocationContextUnavailableError("all_providers_unavailable", "")):
            response = self._view()

        self.assertContains(response, "Search unavailable")
        self.assertEqual(self._rows(), 0)

    def test_the_warming_task_stores_nothing(self) -> None:
        from urbanlens.dashboard.tasks import refresh_pin_web_search

        with mock.patch(self._SEARCH, side_effect=LocationContextUnavailableError("source_error", "refused")):
            refresh_pin_web_search.apply(args=[self.pin.pk])

        self.assertEqual(self._rows(), 0)

    def test_a_genuine_empty_answer_is_cached(self) -> None:
        with mock.patch(self._SEARCH, return_value=[]):
            self._view()

        self.assertGreater(self._rows(), 0)


class ProviderDiscoveryOutageTests(RedataConfiguredMixin, TestCase):
    """Historic registers and site features ask REData which providers cover the point before asking them."""

    _INDEX = (
        "urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway."
        "RedataCapabilitiesGateway.get_capabilities"
    )

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.pin = baker.make(Pin, profile=baker.make(User).profile, location=_hrsh(), parent_pin=None)

    def _sources(self):
        from urbanlens.dashboard.plugins.builtin.redata_historic_registers import HistoricRegisterPanelSource
        from urbanlens.dashboard.plugins.builtin.redata_site_features import SiteFeaturesPanelSource

        return (HistoricRegisterPanelSource(), SiteFeaturesPanelSource())

    def _rows(self, source) -> int:
        return LocationCache.objects.filter(location=self.pin.location, source=source.cache_source).count()

    def test_an_unreadable_index_writes_nothing(self) -> None:
        for source in self._sources():
            with (
                self.subTest(source=source.key),
                mock.patch(self._INDEX, side_effect=LocationContextUnavailableError("source_error", "refused")),
                self.assertRaises(LocationContextUnavailableError),
            ):
                source.fetch(self.pin)
            self.assertEqual(self._rows(source), 0)

    def test_no_coverage_is_cached_as_empty(self) -> None:
        for source in self._sources():
            with self.subTest(source=source.key), mock.patch(self._INDEX, return_value={"domains": []}):
                source.fetch(self.pin)
            self.assertEqual(self._rows(source), 1)


class SourceOutageClassificationTests(SimpleTestCase):
    """What :func:`is_source_outage` calls "could not ask", which every cache write above depends on."""

    def _response(self, status: int) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        return response

    def test_could_not_ask(self) -> None:
        from urbanlens.dashboard.services.core.gateway import is_source_outage
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        for exc in (
            requests.ConnectionError("refused"),
            requests.Timeout("timed out"),
            ConnectionRefusedError(),
            TimeoutError(),
            requests.HTTPError(response=self._response(503)),
            requests.HTTPError(response=self._response(429)),
            requests.HTTPError("no response"),
            GatewayRequestError("Could not reach REData"),
            RateLimitExceededError("nominatim"),
            LocationContextUnavailableError("source_error", "503"),
            PropertyRecordsUnavailableError("source_error", "refused"),
            PropertyRecordsUnavailableError("source_rate_limited", ""),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertTrue(is_source_outage(exc))

    def test_answered(self) -> None:
        from urbanlens.dashboard.services.core.gateway import is_source_outage

        for exc in (
            requests.HTTPError(response=self._response(404)),
            requests.HTTPError(response=self._response(400)),
            ValueError("unparseable"),
            KeyError("results"),
            LocationContextUnavailableError("invalid_coordinates", "", rejected=True),
            PropertyRecordsUnavailableError("no_data_found", ""),
            PropertyRecordsUnavailableError("manual_only", ""),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(is_source_outage(exc))


class AddressBackfillOutageTests(TestCase):
    """The address backfill's marker means "attempted, ever": an outage must not set it."""

    _GEOCODE = "urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway.geocode_coordinates"
    _ADMIN = "urbanlens.dashboard.services.apis.locations.nominatim.NominatimGateway.reverse_geocode_admin"

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude=41.7321, longitude=-73.9262, route=None, locality=None)

    def _enrich(self, *, geocode: dict, admin: dict) -> None:
        from urbanlens.dashboard.services.locations.enrichment import AddressEnrichmentSource

        with (
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "key"),
            mock.patch(self._GEOCODE, **geocode),
            mock.patch(self._ADMIN, **admin),
        ):
            AddressEnrichmentSource().enrich(self.location)

    def _marked(self) -> bool:
        return LocationCache.objects.filter(location=self.location, source="address_backfill").exists()

    def test_an_outage_of_both_sets_no_marker(self) -> None:
        with self.assertRaises(requests.ConnectionError):
            self._enrich(
                geocode={"side_effect": requests.ConnectionError("refused")},
                admin={"side_effect": requests.ConnectionError("refused")},
            )

        self.assertFalse(self._marked())

    def test_a_google_outage_sets_no_marker_though_openstreetmap_filled_the_town(self) -> None:
        """The street only comes from Google, so the marker would end the one chance of getting it."""
        with self.assertRaises(requests.ConnectionError):
            self._enrich(
                geocode={"side_effect": requests.ConnectionError("refused")},
                admin={"return_value": {"city": "Poughkeepsie", "state": "New York", "country": "US"}},
            )

        self.location.refresh_from_db()
        self.assertEqual(self.location.locality, "Poughkeepsie")
        self.assertFalse(self._marked())

    def test_nothing_found_by_either_sets_the_marker(self) -> None:
        self._enrich(geocode={"return_value": {"results": []}}, admin={"return_value": None})

        self.assertTrue(self._marked())
