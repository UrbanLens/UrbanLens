"""Callers treat an environment refusal as "not available here" and move on (D26).

The boundary chain skips the source rather than deferring it; a panel says so rather than caching an empty
answer or vanishing; the name and geocode chains never fall through from REData to a direct provider off
production; the AI features are reserved and logged on production and staging and refused in development; messaging,
Stripe, the hosted basemap and the public Overpass mirrors stay production's.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.core.rate_limiter import EnvironmentRefusedError
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain
from urbanlens.dashboard.services.pins.external_data import (
    get_panel_source,
    run_panel_fetch,
    schedule_panel_fetch,
    unavailable_here,
)

# Imported before any test patches ``ai.factory.get_gateway``: these bind it at import, and one first imported
# under the patch would keep the mock for every later test in the process.
import urbanlens.dashboard.services.trips.trip_ai_suggestions  # noqa: F401
import urbanlens.dashboard.services.trivia  # noqa: F401

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
        """It used to turn every refusal into a rate limit, which the chain defers."""
        from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway

        with deployment("development"), self.assertRaises(EnvironmentRefusedError):
            OvertureMapsGateway()._reserve_call_budget("building")


class OverpassMirrorTests(SimpleTestCase):
    def test_development_asks_only_the_self_hosted_primary(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway

        gateway = OverpassGateway()
        with deployment("development"):
            self.assertEqual(gateway._endpoints(), [gateway.base_url])
        with deployment("production"):
            self.assertGreater(len(gateway._endpoints()), 1)


class OverpassMirrorLedgerTests(TestCase):
    """A failover to a public mirror spends the mirrors' own ``quota`` budget, not the self-hosted primary's."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_failover_is_ledgered_under_the_mirror_service(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries import overpass

        gateway = overpass.OverpassGateway(mirrors=("https://mirror.example/api/interpreter",))

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
    _SOURCE_KEY = "photon"

    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=_LAT, longitude=_LON))
        self.source = get_panel_source(self._SOURCE_KEY)

    def test_a_refused_fetch_says_so_and_stores_nothing(self) -> None:
        with mock.patch.object(type(self.source), "fetch", side_effect=_refused("photon")):
            run_panel_fetch(self._SOURCE_KEY, self.pin, None)

        self.assertEqual(unavailable_here(self._SOURCE_KEY, self.pin), "photon")
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

        cache.set(self.source.unavailable_key(self.pin), "photon", 60)
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
    """REData failing off production is "no name for now", never a call to direct Google Geocoding."""

    def _chain(self, direct: mock.Mock):
        from urbanlens.dashboard.services.locations.google import (
            GoogleGeocodingNameResolver,
            GooglePlacesNameResolver,
            PlaceNameResolverChain,
        )

        return PlaceNameResolverChain(resolvers=(GooglePlacesNameResolver(), GoogleGeocodingNameResolver())), direct

    def _run(self, environment: str) -> mock.Mock:
        with (
            deployment(environment),
            mock.patch("urbanlens.dashboard.services.locations.google._redata_configured", return_value=True),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.places_resolution.resolve_name_from_nearby",
                return_value=None,
            ),
            mock.patch("urbanlens.dashboard.services.locations.google.GoogleGeocodingGateway") as direct,
        ):
            direct.return_value.get_place_name.return_value = "Hudson River State Hospital"
            chain, _ = self._chain(direct)
            name = chain.resolve(_LAT, _LON)
        return name, direct

    def test_staging_does_not_fall_through_to_google(self) -> None:
        name, direct = self._run("staging")
        self.assertIsNone(name)
        direct.assert_not_called()

    def test_production_still_does(self) -> None:
        name, _ = self._run("production")
        self.assertEqual(name, "Hudson River State Hospital")


class GeocodeChainTests(SimpleTestCase):
    _MODULE = "urbanlens.dashboard.services.apis.locations.geocode_resolution"

    def _run(self, environment: str):
        with (
            deployment(environment),
            mock.patch(f"{self._MODULE}.redata_configured", return_value=True),
            mock.patch(f"{self._MODULE}.RedataGeocodeGateway") as redata,
            mock.patch(f"{self._MODULE}.nominatim_geocode", return_value=(1.0, 2.0)) as nominatim,
        ):
            redata.return_value.geocode.side_effect = LocationContextUnavailableError(
                "rate_limited", "budget exhausted"
            )
            from urbanlens.dashboard.services.apis.locations.geocode_resolution import geocode_address

            try:
                return geocode_address("1 Main St"), nominatim
            except LocationContextUnavailableError as exc:
                return exc, nominatim

    def test_a_redata_503_surfaces_as_unavailable_off_production(self) -> None:
        for environment in ("staging", "development"):
            with self.subTest(environment=environment):
                result, nominatim = self._run(environment)
                self.assertIsInstance(result, LocationContextUnavailableError)
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
                side_effect=LocationContextUnavailableError("rate_limited", "x"),
            ),
            self.assertRaises(AddressLookupUnavailableError),
        ):
            coordinates_for_address("1 Main St")

    def test_nominatim_propagates_the_refusal_rather_than_reporting_no_results(self) -> None:
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        with deployment("development"), self.assertRaises(EnvironmentRefusedError):
            NominatimGateway().search("1 Main St", limit=1)


class _FakeGateway:
    model = "fake-model"
    cost = Decimal("0.0010")
    tokens = 0

    def __init__(self, answer: str = "", answers: list[str] | None = None) -> None:
        self.answer = answer
        self.answers = answers or []
        self.calls = 0

    def send_prompt(self, prompt: str, **kwargs) -> str:
        self.calls += 1
        return self.answer

    def send_prompt_list(self, prompt: str, *, max_results: int | None = None, **kwargs) -> list[str]:
        self.calls += 1
        return self.answers


class AiPathsAreReservedAndLoggedTests(TestCase):
    """The seven features that called the provider with no ledger row and no gate (2026-10-05 audit).

    The assistant is covered in ``test_ai_assistant``.
    """

    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _call(self, feature: str):
        gateway = _FakeGateway(answer="", answers=[])
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            getattr(self, f"_run_{feature}")(gateway)
        return gateway

    def _run_trivia_generation(self, gateway: _FakeGateway) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.trivia.generation import MIN_DESCRIPTION_LENGTH, generate_questions_for_wiki

        wiki = baker.make(Wiki, location=baker.make(Location), description="x" * MIN_DESCRIPTION_LENGTH)
        generate_questions_for_wiki(wiki, gateway=gateway)

    def _run_link_extraction(self, gateway: _FakeGateway) -> None:
        from urbanlens.dashboard.models.link_extraction.model import LinkExtraction
        from urbanlens.dashboard.services.ai import link_extraction

        extraction = baker.make(
            LinkExtraction, profile=self.profile, pin=baker.make(Pin, profile=self.profile), url="https://example.com/a"
        )
        with (
            mock.patch.object(link_extraction, "fetch_page_text", return_value="page"),
            mock.patch.object(link_extraction, "build_extraction_prompt", return_value=("i", "p")),
            mock.patch.object(link_extraction, "_notify_extraction_complete"),
        ):
            link_extraction.run_extraction(extraction)

    def _run_document_pin_import(self, gateway: _FakeGateway) -> None:
        from urbanlens.dashboard.services.ai.document_import import extract_pins_from_text

        extract_pins_from_text("a.txt", "Old Mill, 1 Main St", self.profile)

    def _run_trip_suggestions(self, gateway: _FakeGateway) -> None:
        from urbanlens.dashboard.services.trips import trip_ai_suggestions

        with (
            mock.patch.object(trip_ai_suggestions, "get_gateway", return_value=gateway),
            mock.patch.object(trip_ai_suggestions, "build_trip_context"),
            mock.patch.object(trip_ai_suggestions, "_format_prompt", return_value="p"),
        ):
            trip_ai_suggestions.generate_trip_suggestions(mock.Mock(pk=1), self.profile)

    def _run_label_style_suggestions(self, gateway: _FakeGateway) -> None:
        from urbanlens.dashboard.services.labels import style_suggestions

        with mock.patch.object(style_suggestions, "ai_features_enabled", return_value=True):
            style_suggestions.suggest_label_style("Factories", self.profile)

    def _run_category_suggestions(self, gateway: _FakeGateway) -> None:
        from urbanlens.dashboard.services.labels.auto_tag import AutoTagService

        service = AutoTagService()
        with (
            mock.patch.object(AutoTagService, "_build_prompt", return_value="p"),
            mock.patch.object(AutoTagService, "_build_instructions", return_value="i"),
        ):
            service._ai_match(mock.Mock(), [mock.Mock(name="label")], "category")

    FEATURES = (
        "trivia_generation",
        "link_extraction",
        "document_pin_import",
        "trip_suggestions",
        "label_style_suggestions",
        "category_suggestions",
    )

    def test_each_call_is_one_row_under_its_feature(self) -> None:
        for feature in self.FEATURES:
            with self.subTest(feature=feature):
                gateway = self._call(feature)
                self.assertEqual(gateway.calls, 1)
                self.assertEqual(ApiCallLog.objects.filter(service=feature).count(), 1)

    def test_a_feature_switched_off_on_the_api_limits_page_makes_no_call(self) -> None:
        for feature in self.FEATURES:
            with self.subTest(feature=feature):
                ApiRateLimit.objects.update_or_create(
                    service=feature, defaults={"display_name": feature, "enabled": False}
                )
                gateway = self._call(feature)
                self.assertEqual(gateway.calls, 0)

    def test_development_refuses_every_ai_feature_and_records_nothing(self) -> None:
        """Jess, 2026-10-06: dev should not call AI providers. The sweep is covered in ``test_ai_refused_in_development``."""
        for feature in self.FEATURES:
            with self.subTest(feature=feature), deployment("development"):
                gateway = self._call(feature)
                self.assertEqual(gateway.calls, 0)
        self.assertFalse(ApiCallLog.objects.exists())

    def test_staging_still_calls_ai_and_logs_it(self) -> None:
        with deployment("staging"):
            gateway = self._call("document_pin_import")
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(ApiCallLog.objects.filter(service="document_pin_import").count(), 1)

    def test_an_override_opts_one_feature_back_in_on_development(self) -> None:
        with deployment("development", overrides={"document_pin_import": 1.0}):
            gateway = self._call("document_pin_import")
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(ApiCallLog.objects.filter(service="document_pin_import").count(), 1)

    def test_the_ai_features_carry_no_spend_cap(self) -> None:
        """Logged, never refused for spend (R31): no daily or 30-day window, only a per-minute runaway guard."""
        from urbanlens.dashboard.services.core.rate_limiter import SERVICE_REGISTRY

        for feature in (
            "link_extraction",
            "document_pin_import",
            "trip_suggestions",
            "label_style_suggestions",
            "category_suggestions",
            "assistant",
        ):
            with self.subTest(feature=feature):
                self.assertIsNone(SERVICE_REGISTRY[feature].calls_per_day)
                self.assertIsNone(SERVICE_REGISTRY[feature].calls_per_30_days)
                self.assertIsNotNone(SERVICE_REGISTRY[feature].calls_per_minute)

    def test_an_answered_call_with_nothing_usable_in_it_is_a_success(self) -> None:
        """Provider health reads a failed row as the provider failing; an empty answer is the provider answering."""
        from urbanlens.dashboard.services.ai.call_log import recorded_ai_call

        class _Answering(_FakeGateway):
            def send_prompt_list(self, prompt: str, **kwargs) -> list[str]:
                with recorded_ai_call(service="category_suggestions", provider="cloudflare", model="m") as call:
                    call.success = True
                return super().send_prompt_list(prompt, **kwargs)

        gateway = _Answering(answers=[])
        with mock.patch("urbanlens.dashboard.services.ai.factory.get_gateway", return_value=gateway):
            self._run_category_suggestions(gateway)
        self.assertTrue(ApiCallLog.objects.get(service="category_suggestions").success)

    def test_a_shared_gateway_records_each_call_s_own_cost(self) -> None:
        """The trivia sweep reuses one gateway, whose ``cost`` is a running total."""
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.trivia.generation import MIN_DESCRIPTION_LENGTH, generate_questions_for_wiki

        class _Billing(_FakeGateway):
            def send_prompt_list(self, prompt: str, **kwargs) -> list[str]:
                self.cost += Decimal("0.0010")
                return super().send_prompt_list(prompt, **kwargs)

        gateway = _Billing(answers=[])
        for _ in range(3):
            wiki = baker.make(Wiki, location=baker.make(Location), description="x" * MIN_DESCRIPTION_LENGTH)
            generate_questions_for_wiki(wiki, gateway=gateway)
        costs = list(ApiCallLog.objects.filter(service="trivia_generation").values_list("cost_estimate", flat=True))
        self.assertEqual(costs, [Decimal("0.001")] * 3)

    def test_a_refused_trivia_sweep_marks_no_wiki_tried(self) -> None:
        """Nothing was asked, so the wikis wait for the next run, not for RETRY_AFTER."""
        from urbanlens.dashboard.models.trivia.model import TriviaGenerationAttempt
        from urbanlens.dashboard.models.wiki.model import Wiki
        from urbanlens.dashboard.services.trivia import generation

        for _ in range(2):
            baker.make(Wiki, location=baker.make(Location), description="x" * generation.MIN_DESCRIPTION_LENGTH)
        ApiRateLimit.objects.update_or_create(
            service="trivia_generation", defaults={"display_name": "t", "enabled": False}
        )
        gateway = _FakeGateway(answers=[])
        with mock.patch.object(generation, "_gateway", return_value=gateway):
            summary = generation.sweep_wikis_for_generation(batch_size=5)
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(summary["wikis_considered"], 0)
        self.assertFalse(TriviaGenerationAttempt.objects.exists())

    def test_a_refused_trip_suggestion_is_unavailable_not_a_cached_apology(self) -> None:
        ApiRateLimit.objects.update_or_create(
            service="trip_suggestions", defaults={"display_name": "t", "enabled": False}
        )
        from urbanlens.dashboard.services.trips import trip_ai_suggestions

        gateway = _FakeGateway(answer="{}")
        with (
            mock.patch.object(trip_ai_suggestions, "get_gateway", return_value=gateway),
            mock.patch.object(trip_ai_suggestions, "build_trip_context"),
        ):
            result = trip_ai_suggestions.generate_trip_suggestions(mock.Mock(pk=1), self.profile)
        self.assertEqual(gateway.calls, 0)
        self.assertFalse(result.generated)


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


class StripeTests(SimpleTestCase):
    def test_stripe_is_unconfigured_off_production(self) -> None:
        from urbanlens.dashboard.services.billing import stripe_client

        with (
            deployment("staging"),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.stripe_secret_key", "sk_test_x"),
        ):
            self.assertFalse(stripe_client.is_configured())
            with self.assertRaises(ImproperlyConfigured):
                stripe_client.configure()
        with (
            deployment("production"),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.stripe_secret_key", "sk_test_x"),
        ):
            self.assertTrue(stripe_client.is_configured())


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
    def test_the_key_is_read_on_production_only(self) -> None:
        from urbanlens.dashboard.services.core.egress import hosted_basemap_api_key
        from urbanlens.dashboard.services.map.basemap_catalogue import protomaps_theme_for

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.protomaps_api_key", "key"):
            with deployment("staging"):
                self.assertEqual(hosted_basemap_api_key(), "")
                self.assertIsNone(protomaps_theme_for("street"))
            with deployment("production"):
                self.assertEqual(hosted_basemap_api_key(), "key")


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

    def test_a_chained_stripe_reconcile_stops_too(self) -> None:
        from urbanlens.dashboard.tasks import reconcile_unlisted_stripe_subscriptions

        with (
            deployment("development"),
            mock.patch("urbanlens.dashboard.services.billing.stripe_client.is_configured") as configured,
        ):
            self.assertIsNone(reconcile_unlisted_stripe_subscriptions(0.0, 0))
        configured.assert_not_called()

    def test_only_an_external_entry_can_be_gated(self) -> None:
        from urbanlens.dashboard.services.core.egress import external_background_task

        with self.assertRaises(ValueError):
            external_background_task("task-outbox-drain")


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
