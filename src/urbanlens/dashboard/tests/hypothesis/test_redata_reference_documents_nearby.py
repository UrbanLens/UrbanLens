"""Tests for the reference-documents-nearby panel and its gateway method."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.plugins.builtin.redata_reference_documents_nearby import (
    ReferenceDocumentsNearbyPanelSource,
    ReferenceDocumentsNearbyPlugin,
)
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)
from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import (
    RedataReferenceDocumentsGateway,
)
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.pins.external_data import get_panel_source

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

_GATEWAY_MODULE = "urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway"


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _gateway(session: mock.Mock) -> RedataReferenceDocumentsGateway:
    return RedataReferenceDocumentsGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


def _wikipedia_doc(**overrides) -> dict:
    return {
        "provider": "wikipedia",
        "kind": "article",
        "title": "Bannerman Castle",
        "description": "Bannerman Castle is a ruined structure on Pollepel Island in the Hudson River.",
        "url": "https://en.wikipedia.org/wiki/Bannerman_Castle",
        "thumbnail_url": "https://example.test/thumb.jpg",
        "date_text": "",
        "creator": "Wikipedia contributors",
        "license": "CC BY-SA 4.0",
        "distance_meters": 42.0,
        "attributes": {"pageid": 123, "language": "en"},
        **overrides,
    }


def _wikidata_doc(**overrides) -> dict:
    return {
        "provider": "wikidata",
        "kind": "other",
        "title": "Bannerman Castle",
        "description": "castle-like structure in New York",
        "url": "https://www.wikidata.org/wiki/Q4859028",
        "thumbnail_url": "",
        "date_text": "1901-01-01",
        "creator": "Francis Bannerman VI",
        "license": "CC0 1.0",
        "distance_meters": 10.0,
        "attributes": {
            "wikidata_id": "Q4859028",
            "instance_of": "castle",
            "architectural_style": "Scottish Baronial",
            "heritage_designation": "National Register of Historic Places",
        },
        **overrides,
    }


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class GetReferenceDocumentsTests(SimpleTestCase):
    """``get_reference_documents`` delegates to the shared near-point helper."""

    def test_hits_the_near_point_path_not_search(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_reference_documents(41.5, -74.0)

        url = session.get.call_args.args[0]
        self.assertEqual(url, "https://redata.example.test/api/v1/reference-documents/")

    def test_sends_lat_lng(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_reference_documents(41.5, -74.0)

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["lat"], 41.5)
        self.assertEqual(params["lng"], -74.0)
        self.assertNotIn("q", params)

    def test_radius_and_provider_and_force_refresh_are_forwarded(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(200, {"count": 0, "complete": True, "results": [], "providers": []})

        _gateway(session).get_reference_documents(
            41.5, -74.0, radius_meters=500, provider=["wikipedia", "wikidata"], force_refresh=True
        )

        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["radius_meters"], 500)
        self.assertEqual(params["provider"], ["wikipedia", "wikidata"])
        self.assertEqual(params["force_refresh"], "true")

    def test_200_returns_the_parsed_envelope(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            200,
            {
                "count": 2,
                "complete": True,
                "results": [_wikipedia_doc(), _wikidata_doc()],
                "providers": [{"provider": "wikipedia", "status": "ok"}, {"provider": "wikidata", "status": "ok"}],
            },
        )

        envelope = _gateway(session).get_reference_documents(41.5, -74.0)

        self.assertIsInstance(envelope, LocationContextEnvelope)
        self.assertEqual(envelope.count, 2)
        self.assertTrue(envelope.complete)
        self.assertEqual(len(envelope.results), 2)

    def test_rate_limited_503_raises(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(
            503, {"error": "rate_limited", "message": "Wikidata's query service is rate-limiting right now."}
        )

        with pytest.raises(LocationContextUnavailableError) as excinfo:
            _gateway(session).get_reference_documents(41.5, -74.0)
        self.assertEqual(excinfo.value.reason, "rate_limited")

    def test_shares_the_search_methods_service_key(self) -> None:
        """Both endpoints share one rate-limit budget - see the module docstring."""
        self.assertEqual(RedataReferenceDocumentsGateway.service_key, "redata_reference_documents")


# ---------------------------------------------------------------------------
# Panel: render_context
# ---------------------------------------------------------------------------


def _us_pin() -> Pin:
    location = baker.make(Location, latitude=41.5, longitude=-74.0)
    return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)


class RenderContextTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = ReferenceDocumentsNearbyPanelSource()
        self.pin = _us_pin()

    def test_empty_documents_hides_the_panel(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"documents": []}))

    def test_happy_path_both_providers(self) -> None:
        data = {"documents": [_wikipedia_doc(), _wikidata_doc()]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Bannerman Castle")
        self.assertIn("castle", ctx["chips"])
        self.assertEqual(
            ctx["footer_link"], {"url": "https://en.wikipedia.org/wiki/Bannerman_Castle", "label": "View on Wikipedia"}
        )

    def test_wikidata_claims_become_labelled_meta_rows(self) -> None:
        """The exact field mapping from REData's serializer - see the plugin's _CLAIM_META."""
        data = {"documents": [_wikidata_doc()]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        by_label = {row["label"]: row["value"] for row in ctx["meta"]}
        self.assertEqual(by_label["Built"], "1901-01-01")
        self.assertEqual(by_label["Designer"], "Francis Bannerman VI")
        self.assertEqual(by_label["Style"], "Scottish Baronial")
        self.assertEqual(by_label["Heritage status"], "National Register of Historic Places")

    def test_date_text_is_never_reparsed(self) -> None:
        """Archival dating is fuzzy on purpose (see ReferenceDocumentSerializer's own
        docstring) - a provider string like "c. 1890" must survive verbatim."""
        data = {"documents": [_wikidata_doc(date_text="c. 1890")]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(next(row["value"] for row in ctx["meta"] if row["label"] == "Built"), "c. 1890")

    def test_wikidata_only_falls_back_to_entity_title_and_no_footer_link(self) -> None:
        data = {"documents": [_wikidata_doc()]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Bannerman Castle")
        self.assertIsNone(ctx["footer_link"])

    def test_wikidata_entity_with_degraded_claims_still_renders_via_wikipedia(self) -> None:
        """REData's claim-enrichment query can fail independently of the entity search - the entity comes back labelled but with every claim field blank, a normal 200, not an error (see WikidataGateway's own docstring). The card must still render from the Wikipedia half rather than disappearing."""
        degraded_entity = _wikidata_doc(date_text="", creator="", attributes={"wikidata_id": "Q4859028"})
        data = {"documents": [_wikipedia_doc(), degraded_entity]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"], [])
        self.assertEqual(ctx["chips"], [])
        self.assertEqual(ctx["heading_name"], "Bannerman Castle")
        assert ctx["footer_link"] is not None
        self.assertEqual(ctx["footer_link"]["url"], "https://en.wikipedia.org/wiki/Bannerman_Castle")

    def test_blank_claim_fields_are_omitted_not_shown_empty(self) -> None:
        data = {"documents": [_wikidata_doc(date_text="", attributes={"wikidata_id": "Q1", "instance_of": "castle"})]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = [row["label"] for row in ctx["meta"]]
        self.assertNotIn("Built", labels)

    def test_nearest_wikipedia_article_wins_when_more_than_one(self) -> None:
        """REData returns each provider's own rows nearest-first."""
        near = _wikipedia_doc(title="Near Article", distance_meters=5.0)
        far = _wikipedia_doc(title="Far Article", url="https://en.wikipedia.org/wiki/Far", distance_meters=900.0)
        data = {"documents": [near, far]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Near Article")

    def test_non_dict_documents_are_skipped_defensively(self) -> None:
        data = {"documents": ["not-a-dict", _wikipedia_doc()]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Bannerman Castle")


# ---------------------------------------------------------------------------
# Panel: api_info (the API-only "description" field)
# ---------------------------------------------------------------------------


class ApiInfoTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = ReferenceDocumentsNearbyPanelSource()
        self.pin = _us_pin()

    def test_description_is_the_wikipedia_intro_extract(self) -> None:
        data = {"documents": [_wikipedia_doc(), _wikidata_doc()]}
        card = self.source.api_info(self.pin, data)
        assert card is not None
        self.assertEqual(
            card["description"], "Bannerman Castle is a ruined structure on Pollepel Island in the Hudson River."
        )

    def test_wikidata_only_has_no_description(self) -> None:
        data = {"documents": [_wikidata_doc()]}
        card = self.source.api_info(self.pin, data)
        assert card is not None
        self.assertIsNone(card["description"])

    def test_meta_and_chips_still_present_on_the_api_card(self) -> None:
        data = {"documents": [_wikipedia_doc(), _wikidata_doc()]}
        card = self.source.api_info(self.pin, data)
        assert card is not None
        self.assertIn("castle", card["chips"])
        self.assertTrue(card["meta"])

    def test_empty_documents_yields_no_card(self) -> None:
        self.assertIsNone(self.source.api_info(self.pin, {"documents": []}))


# ---------------------------------------------------------------------------
# Panel: fetch / outage handling
# ---------------------------------------------------------------------------


class FetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = ReferenceDocumentsNearbyPanelSource()
        self.pin = _us_pin()

    def _cached(self):
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        return LocationCache.get_fresh(self.pin.location, self.source.cache_source)

    def test_fetch_calls_the_near_point_method_not_search(self) -> None:
        with mock.patch(f"{_GATEWAY_MODULE}.RedataReferenceDocumentsGateway") as gateway_cls:
            gateway_cls.return_value.get_reference_documents.return_value = LocationContextEnvelope(
                count=0, complete=True, results=[]
            )
            self.source.fetch(self.pin)
        gateway_cls.return_value.get_reference_documents.assert_called_once()
        gateway_cls.return_value.search.assert_not_called()

    def test_fetch_caches_the_results_under_documents(self) -> None:
        envelope = LocationContextEnvelope(count=1, complete=True, results=[_wikipedia_doc()])
        with mock.patch(f"{_GATEWAY_MODULE}.RedataReferenceDocumentsGateway") as gateway_cls:
            gateway_cls.return_value.get_reference_documents.return_value = envelope
            self.source.fetch(self.pin)

        cached = self._cached()
        assert cached is not None
        self.assertEqual(cached.data["documents"][0]["title"], "Bannerman Castle")

    def test_a_genuine_empty_result_is_cached(self) -> None:
        """Asked and told nothing (complete=True, results=[]) is a real, cacheable answer."""
        envelope = LocationContextEnvelope(count=0, complete=True, results=[])
        with mock.patch(f"{_GATEWAY_MODULE}.RedataReferenceDocumentsGateway") as gateway_cls:
            gateway_cls.return_value.get_reference_documents.return_value = envelope
            self.source.fetch(self.pin)

        self.assertIsNotNone(self._cached())

    def test_a_total_blackout_is_not_cached(self) -> None:
        """complete=False with no results at all means 'could not ask' - must stay retryable.

        Exercises RedataInfoPanelSource's shared outage rule against this panel's own gateway call - see
        test_outage_not_cached_as_empty.py for the general case."""
        envelope = LocationContextEnvelope(count=0, complete=False, results=[])
        with mock.patch(f"{_GATEWAY_MODULE}.RedataReferenceDocumentsGateway") as gateway_cls:
            gateway_cls.return_value.get_reference_documents.return_value = envelope
            self.source.fetch(self.pin)

        self.assertIsNone(self._cached(), "an outage must not be cached as emptiness - nothing would ever retry it")

    def test_a_partial_outage_with_real_results_is_still_cached(self) -> None:
        """Wikidata down, Wikipedia up: complete=False but results is non-empty - REData's
        contract says this was 'asked and answered', not 'could not ask'."""
        envelope = LocationContextEnvelope(
            count=1,
            complete=False,
            results=[_wikipedia_doc()],
            providers=[{"provider": "wikidata", "status": "rate_limited"}, {"provider": "wikipedia", "status": "ok"}],
        )
        with mock.patch(f"{_GATEWAY_MODULE}.RedataReferenceDocumentsGateway") as gateway_cls:
            gateway_cls.return_value.get_reference_documents.return_value = envelope
            self.source.fetch(self.pin)

        cached = self._cached()
        assert cached is not None
        self.assertEqual(len(cached.data["documents"]), 1)

    def test_a_hard_gateway_failure_propagates(self) -> None:
        with mock.patch(f"{_GATEWAY_MODULE}.RedataReferenceDocumentsGateway") as gateway_cls:
            gateway_cls.return_value.get_reference_documents.side_effect = LocationContextUnavailableError(
                "source_error", "down"
            )
            with pytest.raises(LocationContextUnavailableError):
                self.source.fetch(self.pin)

    def test_is_gated_behind_the_places_feature(self) -> None:
        """Decided 2026-09-08: a near-a-coordinate search is inherently about somewhere other than the pin's own place - see the module docstring and test_panel_feature_gate.py for the framework this relies on."""
        from urbanlens.dashboard.models.subscriptions import SiteFeature

        self.assertEqual(self.source.required_feature, SiteFeature.PLACES)


# ---------------------------------------------------------------------------
# Registration and rate limiting
# ---------------------------------------------------------------------------


class RegistrationTests(TestCase):
    def test_panel_is_registered(self) -> None:
        self.assertIsNotNone(get_panel_source("redata_reference_documents_nearby"))

    def test_key_does_not_collide_with_the_search_based_media_panels(self) -> None:
        self.assertNotEqual(ReferenceDocumentsNearbyPanelSource.key, "redata_reference_documents")


class RateLimitTests(TestCase):
    def test_the_plugin_declares_the_gateways_shared_service_key(self) -> None:
        """The rate budget belongs to the gateway (shared with the Media gallery's
        search-based archive providers), not to this panel's own cache_source."""
        self.assertIn("redata_reference_documents", ReferenceDocumentsNearbyPlugin().get_service_defaults())

    def test_get_limit_config_uses_the_declared_defaults(self) -> None:
        config = rate_limiter.get_limit_config("redata_reference_documents")
        self.assertEqual(config.display_name, "REData Reference Documents")
        self.assertEqual(config.calls_per_minute, 20)
        self.assertIsNone(config.calls_per_day)
        self.assertNotEqual(config.notes, "")


# ---------------------------------------------------------------------------
# Subscription gating (end-to-end, mirrors test_panel_feature_gate.py's
# real-example pattern for EPA ECHO/Incident History)
# ---------------------------------------------------------------------------


class PanelDispatchGatingTests(TestCase):
    """``PinController.panel`` actually refuses this panel to a non-subscriber."""

    def setUp(self) -> None:
        super().setUp()
        from django.contrib.auth.models import User as DjangoUser

        # See test_panel_feature_gate.py's own GatedPanelServingTests.setUp docstring:
        # the first user in a fresh test DB is auto-promoted to site admin, and a site
        # admin holds every SiteFeature - a throwaway user absorbs that.
        baker.make(DjangoUser)
        self.pin = _us_pin()
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(self.pin.location, "redata_reference_documents_nearby", {"documents": [_wikipedia_doc()]})

    def test_unsubscribed_viewer_gets_404(self) -> None:
        from django.urls import reverse

        self.client.force_login(self.pin.profile.user)
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "redata_reference_documents_nearby"]))
        self.assertEqual(response.status_code, 404)

    def test_subscriber_with_places_feature_gets_the_panel(self) -> None:
        from django.urls import reverse

        from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription

        role = baker.make(SubscriptionRole, features=SiteFeature.PLACES)
        grant_subscription(self.pin.profile.user, role, self.pin.profile.user, None)
        self.client.force_login(self.pin.profile.user)

        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "redata_reference_documents_nearby"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bannerman Castle")
