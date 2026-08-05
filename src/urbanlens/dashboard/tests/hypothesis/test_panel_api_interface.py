"""Tests for PanelSource's JSON read interface (``api_kinds`` / ``api_payload``).

The pin-detail panels are a plugin extension point, so the set of classes
reaching this interface is open-ended and includes code this repository will
never see. That makes two properties worth pinning down hard, because a
regression in either is silent:

1. **It fails closed.** A source that says nothing about the API is absent from
   the API. Not "renders empty", not "dumps its cache row" - absent. A future
   plugin author who never reads ``external_data.py`` must not be able to
   publish whatever their ``fetch`` happened to cache by doing nothing.
2. **The uniform base classes opt in on purpose, and only through their own
   contracts.** ``InfoPanelSource`` derives its payload from ``render_context``
   and ``GalleryMediaSource`` from ``media_items`` - and the info projection is
   an allowlist, so a presentation-only context key never leaks.

The rest covers the bulk readiness helper (which exists to kill an N+1 and is
therefore tested for query *count*, not just correctness) and the hand-written
payloads on the bespoke sources, each of which has to reproduce its web panel's
own emptiness rule so the two surfaces agree on when a panel has nothing to say.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.plugins.builtin.azure_maps import AzureMapsPanelSource
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.plugins.builtin.epa_echo import EpaEchoDetailPanelSource, EpaEchoNearbyPanelSource
from urbanlens.dashboard.plugins.builtin.nominatim import NominatimPanelSource, wikipedia_url
from urbanlens.dashboard.plugins.builtin.nps import NpsPanelSource
from urbanlens.dashboard.plugins.builtin.parcel_buildings import ParcelBuildingsPanelSource, building_footprint_geojson
from urbanlens.dashboard.plugins.builtin.usgs import UsgsTopoPanelSource
from urbanlens.dashboard.services.pins.external_data import (
    BoundaryPanelSource,
    GalleryMediaSource,
    InfoPanelSource,
    PanelApiKind,
    PanelSource,
    SatellitePanelSource,
    StreetViewPanelSource,
    get_panel_source,
    info_card,
    info_card_from_render_context,
    panel_readiness,
    panel_sources,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

#: A square footprint around the recipe location's first coordinate pair, used
#: wherever a test needs a *real* building outline rather than the degenerate
#: Point REData sends when a county published none.
_FOOTPRINT = {"type": "Polygon", "coordinates": [[[-74.0015, 40.0005], [-74.0005, 40.0005], [-74.0005, 40.0015], [-74.0015, 40.0015], [-74.0015, 40.0005]]]}


class _StubPanelSource(PanelSource):
    """A minimal third-party-style source whose author never thought about the API."""

    key = "stub_forgot_the_api"

    def is_ready(self, pin: Pin) -> bool:
        """Always ready - this stub exists to test the API contract, not fetching."""
        return True

    def fetch(self, pin: Pin) -> None:
        """No-op: nothing upstream to call."""


class PanelApiFailClosedTests(SimpleTestCase):
    """The default contract: silence means "not on the API"."""

    def test_bare_source_declares_no_kinds(self) -> None:
        """A plugin author who ignores the interface publishes nothing."""
        self.assertEqual(_StubPanelSource.api_kinds, frozenset())

    def test_bare_source_returns_no_payload(self) -> None:
        """The second, independent lock: even a source that declared kinds by
        mistake still has to write a payload before anything is emitted."""
        self.assertIsNone(_StubPanelSource().api_payload(None))  # type: ignore[arg-type]

    def test_bare_source_has_no_required_feature(self) -> None:
        """Unrestricted is the default; gating is opt-in and explicit."""
        self.assertIsNone(_StubPanelSource.required_feature)

    def test_info_panel_sources_opt_in_to_info(self) -> None:
        """The uniform info base class exposes its subclasses for free."""
        self.assertEqual(InfoPanelSource.api_kinds, frozenset({PanelApiKind.INFO}))

    def test_gallery_media_sources_opt_in_to_media(self) -> None:
        """Same for the uniform media base class."""
        self.assertEqual(GalleryMediaSource.api_kinds, frozenset({PanelApiKind.MEDIA}))

    def test_imagery_carousels_are_deliberately_excluded(self) -> None:
        """Regression guard for the base64-slide payload-size decision.

        Satellite and street-view slides carry base64 ``data:`` URIs fetched
        server-side; several providers x ~5 slides is plausibly 5-15 MB in one
        response, and the external API's throttle counts requests, not bytes.
        They stay off the API until a signed slide-image proxy exists - if
        someone "helpfully" wires them up, this fails.
        """
        for source in (SatellitePanelSource(), StreetViewPanelSource()):
            with self.subTest(source=source.key):
                self.assertEqual(source.api_kinds, frozenset())
                self.assertIsNone(source.api_payload(None))  # type: ignore[arg-type]


class PanelApiRegistryConsistencyTests(TestCase):
    """Properties that must hold across every registered source, plugins included."""

    def test_every_source_declaring_a_kind_overrides_api_payload(self) -> None:
        """Declaring a shape without writing a body would advertise an always-empty panel."""
        for key, source in panel_sources().items():
            if not source.api_kinds:
                continue
            with self.subTest(source=key):
                self.assertIsNot(
                    type(source).api_payload,
                    PanelSource.api_payload,
                    f"{key} declares api_kinds but inherits PanelSource's None payload",
                )

    def test_declared_kinds_are_known_values(self) -> None:
        """A typo'd kind would be an unrenderable string on every client."""
        for key, source in panel_sources().items():
            with self.subTest(source=key):
                self.assertTrue(set(source.api_kinds) <= set(PanelApiKind))

    def test_required_feature_is_only_set_where_the_web_gates_too(self) -> None:
        """The gated set is exactly EPA's nearby-facility list, today.

        This is a "notice when it changes" test rather than a rule: adding a
        gated panel is fine, but it has to be a deliberate edit here, because
        ``PinController._NEARBY_RESEARCH_TABS`` needs the matching entry or the
        web and the API will disagree about who may see it.
        """
        gated = {key for key, source in panel_sources().items() if source.required_feature is not None}
        self.assertEqual(gated, {"epa_echo"})


class InfoPanelApiPayloadTests(TestCase):
    """``InfoPanelSource``'s derived payload, exercised through a real plugin."""

    def setUp(self) -> None:
        """A pin with a fresh Photon cache row - the simplest real info panel."""
        super().setUp()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        self.source = get_panel_source("photon")
        assert isinstance(self.source, InfoPanelSource)

    def test_no_cache_row_yields_none(self) -> None:
        """"Not fetched yet" is None, so the caller knows to schedule a fetch."""
        self.assertIsNone(self.source.api_payload(self.pin))

    def test_payload_is_derived_from_render_context(self) -> None:
        """The same facts the web card shows, under the info key."""
        LocationCache.set(self.pin.location, "photon", {"locality": "Poughkeepsie", "region": "New York", "country": "United States"}, query_key="")
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        self.assertEqual(payload[PanelApiKind.INFO.value]["heading_name"], "Poughkeepsie")
        self.assertIn({"label": "Region", "value": "New York"}, payload[PanelApiKind.INFO.value]["meta"])

    def test_empty_render_context_yields_none(self) -> None:
        """A settled-but-useless result is omitted, mirroring the web panel's 204."""
        LocationCache.set(self.pin.location, "photon", {}, query_key="")
        self.assertIsNone(self.source.api_payload(self.pin))

    def test_presentation_only_context_keys_are_not_published(self) -> None:
        """Regression guard for the allowlist projection.

        ``nested`` is a template layout flag. If the projection ever becomes a
        straight copy of the render context, it - and anything else a plugin
        stashes there - starts appearing in the API response.
        """
        card = info_card_from_render_context({"heading_name": "X", "nested": True, "internal_raw_response": {"secret": 1}})
        self.assertNotIn("nested", card)
        self.assertNotIn("internal_raw_response", card)


class GalleryMediaApiPayloadTests(TestCase):
    """``GalleryMediaSource``'s derived payload, exercised through a real provider."""

    def setUp(self) -> None:
        """A pin plus the Smithsonian media provider."""
        super().setUp()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        self.source = get_panel_source("smithsonian")
        assert isinstance(self.source, GalleryMediaSource)

    def test_no_cache_row_yields_none(self) -> None:
        """Distinguishable from "fetched, found nothing" below."""
        self.assertIsNone(self.source.api_payload(self.pin))

    def test_empty_result_is_a_real_answer(self) -> None:
        """An empty list means the search ran - the caller must not re-schedule a fetch."""
        LocationCache.set(self.pin.location, self.source.cache_source, {"items": []}, query_key="q")
        payload = self.source.api_payload(self.pin)
        self.assertEqual(payload, {PanelApiKind.MEDIA.value: []})

    def test_items_serialize_field_for_field(self) -> None:
        """Every MediaItem field survives the trip to JSON."""
        item = {"url": "https://example.test/a.jpg", "thumb_url": "https://example.test/t.jpg", "caption": "A scan", "source": "Smithsonian", "page_url": "https://example.test/a"}
        LocationCache.set(self.pin.location, self.source.cache_source, {"items": [item]}, query_key="q")
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        self.assertEqual(payload[PanelApiKind.MEDIA.value], [item])


class BoundaryApiPayloadTests(TestCase):
    """The boundary panel's geometry payload and its honesty flags."""

    def setUp(self) -> None:
        """A plain pin; boundary rows are added per-test."""
        super().setUp()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        self.source = BoundaryPanelSource()

    def test_declares_the_boundary_kind(self) -> None:
        """Neither an info card nor media - its own shape."""
        self.assertEqual(self.source.api_kinds, frozenset({PanelApiKind.BOUNDARY}))

    def test_synthesized_circle_is_flagged_as_a_fallback(self) -> None:
        """Regression guard: a client must never draw the circle as a parcel line.

        With no real geometry anywhere, ``resolve_for_pin`` synthesizes a
        fixed-radius circle so the map has something to show. Emitting that
        unflagged would assert a property boundary this app never looked up.
        """
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        property_side = payload[PanelApiKind.BOUNDARY.value]["property"]
        self.assertEqual(property_side["source"], "circle")
        self.assertTrue(property_side["is_fallback_circle"])
        self.assertEqual(property_side["geometry"]["type"], "Polygon")

    def test_building_side_is_none_without_a_building_boundary(self) -> None:
        """Buildings have no circle fallback - absent means "no known building"."""
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        self.assertIsNone(payload[PanelApiKind.BOUNDARY.value]["building"])

    def test_pin_without_a_location_yields_none(self) -> None:
        """Nothing to resolve against, so nothing is claimed."""
        self.pin.location = None
        self.assertIsNone(self.source.api_payload(self.pin))


class PanelReadinessTests(TestCase):
    """The bulk readiness helper - correctness and, crucially, query count."""

    def setUp(self) -> None:
        """A pin with one fresh cache row, so readiness is genuinely mixed."""
        super().setUp()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        LocationCache.set(self.pin.location, "photon", {"name": "Somewhere"}, query_key="")

    def test_agrees_with_each_sources_own_is_ready(self) -> None:
        """The batched answer must equal the naive one, source for source."""
        sources = list(panel_sources().values())
        readiness = panel_readiness(self.pin, sources)
        for source in sources:
            with self.subTest(source=source.key):
                self.assertEqual(readiness[source.key], source.is_ready(self.pin))

    def test_covers_every_requested_source_key(self) -> None:
        """Callers index the result directly, so no key may be missing."""
        sources = list(panel_sources().values())
        self.assertEqual(set(panel_readiness(self.pin, sources)), {source.key for source in sources})

    def test_cache_backed_sources_cost_a_constant_number_of_queries(self) -> None:
        """The whole point: readiness for N panels is not N queries.

        One query for the site's cache-age setting plus one for the location's
        fresh rows. Asking each source individually is one query *per source*,
        on every pin detail page render.
        """
        cache_backed = [source for source in panel_sources().values() if hasattr(source, "cache_source")]
        self.assertGreater(len(cache_backed), 5)
        with self.assertNumQueries(2):
            panel_readiness(self.pin, cache_backed)

    def test_pin_without_a_location_is_ready_for_nothing(self) -> None:
        """No location means no cache rows can exist - and no crash looking."""
        self.pin.location = None
        cache_backed = [source for source in panel_sources().values() if hasattr(source, "cache_source")]
        self.assertFalse(any(panel_readiness(self.pin, cache_backed).values()))

    def test_defaults_to_every_registered_source(self) -> None:
        """Called without a subset, it reports on the whole registry."""
        self.assertEqual(set(panel_readiness(self.pin)), set(panel_sources()))


class ParcelBuildingsApiPayloadTests(TestCase):
    """The buildings payload: child-pin pairing and the geometry flag."""

    def setUp(self) -> None:
        """A root pin whose parcel has two cached buildings."""
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.source = ParcelBuildingsPanelSource()
        self.buildings = [
            {"name": "Powerhouse", "building_number": "9", "source": "cris", "latitude": 40.0010, "longitude": -74.0010, "geometry": _FOOTPRINT},
            {"name": "Tool Shed", "building_number": "154", "source": "osm", "latitude": 41.5, "longitude": -75.5},
        ]

    def _cache(self, provider: str = "redata") -> None:
        """Land a parcel-buildings cache row for the pin's location."""
        LocationCache.set(self.pin.location, self.source.cache_source, {"buildings": self.buildings, "provider": provider}, query_key="q")

    def test_declares_the_buildings_kind(self) -> None:
        """A building row is neither an info card nor a media item."""
        self.assertEqual(self.source.api_kinds, frozenset({PanelApiKind.BUILDINGS}))

    def test_no_cache_row_yields_none(self) -> None:
        """Pending, not empty."""
        self.assertIsNone(self.source.api_payload(self.pin))

    def test_empty_building_list_yields_none(self) -> None:
        """A parcel with no structures has no panel on either surface."""
        LocationCache.set(self.pin.location, self.source.cache_source, {"buildings": []}, query_key="q")
        self.assertIsNone(self.source.api_payload(self.pin))

    def test_child_pin_returns_none_from_the_gate(self) -> None:
        """A detail pin has no sub-buildings, so it is not offered the panel."""
        child: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, parent_pin=self.pin)
        LocationCache.set(child.location, self.source.cache_source, {"buildings": self.buildings}, query_key="q")
        self.assertIsNone(self.source.api_payload(child))

    def test_unpinned_buildings_carry_no_child_pin(self) -> None:
        """Nothing covers either footprint yet, so both rows are unpinned."""
        self._cache()
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        rows = payload[PanelApiKind.BUILDINGS.value]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["child_pin_uuid"] is None and row["child_pin_name"] is None for row in rows))
        self.assertEqual(payload["unpinned_count"], 2)

    def test_covering_child_pin_is_reported_by_uuid_and_name(self) -> None:
        """The pairing a client cannot compute for itself, resolved server-side.

        Without it a mobile client would offer to create a duplicate pin for a
        building someone has already pinned.
        """
        covering_location = baker.make("dashboard.Location", latitude=40.0010, longitude=-74.0010, official_name="Powerhouse pin")
        child: Pin = baker.make_recipe("dashboard.pin", profile=self.profile, parent_pin=self.pin, location=covering_location)
        self._cache()
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        powerhouse = next(row for row in payload[PanelApiKind.BUILDINGS.value] if row["name"] == "Powerhouse")
        self.assertEqual(powerhouse["child_pin_uuid"], str(child.uuid))
        self.assertEqual(powerhouse["child_pin_name"], child.effective_name)
        self.assertEqual(payload["unpinned_count"], 1)

    def test_real_footprint_is_flagged_and_included(self) -> None:
        """A client that draws outlines gets one; the flag saves it from looking."""
        self._cache()
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        powerhouse = next(row for row in payload[PanelApiKind.BUILDINGS.value] if row["name"] == "Powerhouse")
        self.assertTrue(powerhouse["has_geometry"])
        self.assertEqual(powerhouse["geometry"], _FOOTPRINT)

    def test_missing_footprint_is_flagged_absent(self) -> None:
        """The OSM fallback publishes no geometry at all."""
        self._cache()
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        shed = next(row for row in payload[PanelApiKind.BUILDINGS.value] if row["name"] == "Tool Shed")
        self.assertFalse(shed["has_geometry"])
        self.assertIsNone(shed["geometry"])

    def test_provider_is_reported(self) -> None:
        """Which of REData/OSM answered is part of how much to trust the list."""
        self._cache(provider="osm")
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        self.assertEqual(payload["provider"], "osm")


class BuildingFootprintGeojsonTests(SimpleTestCase):
    """The "is this a real outline?" test that backs ``has_geometry``."""

    def test_point_placeholder_is_dropped(self) -> None:
        """REData sends a Point when the county published no outline.

        Keeping it would double a hundred-building payload to repeat the
        latitude/longitude already on the row.
        """
        self.assertIsNone(building_footprint_geojson({"geometry": {"type": "Point", "coordinates": [-74.0, 40.0]}}))

    def test_absent_geometry_is_dropped(self) -> None:
        """OSM-sourced records carry none at all."""
        self.assertIsNone(building_footprint_geojson({"name": "Shed"}))

    def test_non_dict_geometry_is_dropped(self) -> None:
        """A malformed upstream value must not reach a client as geometry."""
        self.assertIsNone(building_footprint_geojson({"geometry": "POLYGON((0 0))"}))

    def test_real_polygon_is_kept(self) -> None:
        """The case the flag exists for."""
        self.assertEqual(building_footprint_geojson({"geometry": _FOOTPRINT}), _FOOTPRINT)


class CrisBuildingApiPayloadTests(TestCase):
    """The one source that is honestly both an info card and a media provider."""

    def setUp(self) -> None:
        """A pin and the CRIS panel source."""
        super().setUp()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        self.source = CrisBuildingPanelSource()

    def test_declares_both_kinds(self) -> None:
        """Regression guard for the MRO trap.

        ``CrisBuildingPanelSource`` inherits from an info base and a media
        base; Python resolves ``api_kinds`` to the first, silently dropping the
        media half, unless the class declares it explicitly.
        """
        self.assertEqual(self.source.api_kinds, frozenset({PanelApiKind.INFO, PanelApiKind.MEDIA}))

    def test_no_cache_row_yields_none(self) -> None:
        """Pending, not empty."""
        self.assertIsNone(self.source.api_payload(self.pin))

    def test_record_with_attachments_yields_both_halves(self) -> None:
        """One cached row, read once, projected into both shapes."""
        LocationCache.set(
            self.pin.location,
            self.source.cache_source,
            {
                "USNName": "Tool Shed",
                "USNNum": "01234.000001",
                "EligibilityDesc": "Eligible",
                "resource_uuid": "abc-123",
                "attachments": [{"id": 7, "kind": "PHOTO", "name": "Front elevation"}],
            },
            query_key="q",
        )
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        self.assertEqual(payload[PanelApiKind.INFO.value]["heading_name"], "Tool Shed")
        self.assertEqual(len(payload[PanelApiKind.MEDIA.value]), 1)

    def test_attachment_urls_stay_on_the_in_app_proxy(self) -> None:
        """REData's API key must never reach a client, on either surface."""
        LocationCache.set(
            self.pin.location,
            self.source.cache_source,
            {"USNName": "Tool Shed", "resource_uuid": "abc-123", "attachments": [{"id": 7, "kind": "PHOTO", "name": "Front"}]},
            query_key="q",
        )
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        url = payload[PanelApiKind.MEDIA.value][0]["url"]
        self.assertNotIn("redata", url)
        self.assertTrue(url.startswith("/"))

    def test_district_only_record_still_serves_its_media(self) -> None:
        """A location inside a historic district with no surveyed building of
        its own has no info card, but may still have attachments worth serving."""
        LocationCache.set(
            self.pin.location,
            self.source.cache_source,
            {"resource_uuid": "abc-123", "attachments": [{"id": 7, "kind": "PHOTO", "name": "Front"}]},
            query_key="q",
        )
        payload = self.source.api_payload(self.pin)
        assert payload is not None
        self.assertIsNone(payload[PanelApiKind.INFO.value])
        self.assertEqual(len(payload[PanelApiKind.MEDIA.value]), 1)

    def test_empty_record_yields_none(self) -> None:
        """Neither half - the panel is absent, not empty."""
        LocationCache.set(self.pin.location, self.source.cache_source, {}, query_key="q")
        self.assertIsNone(self.source.api_payload(self.pin))


class BespokeInfoPanelApiPayloadTests(TestCase):
    """NPS / Nominatim / Azure Maps / USGS - hand-written payloads, shared contract."""

    def setUp(self) -> None:
        """One pin, reused by every source below."""
        super().setUp()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def _cache(self, source: Any, data: dict) -> None:
        """Land a cache row for one panel source."""
        LocationCache.set(self.pin.location, source.cache_source, data, query_key="q")

    def test_nps_outside_every_park_yields_none(self) -> None:
        """A cached ``{}`` means "we checked, it isn't in a park" - a settled no."""
        source = NpsPanelSource()
        self._cache(source, {})
        self.assertIsNone(source.api_payload(self.pin))

    def test_nps_card_carries_name_photo_and_prose(self) -> None:
        """The three things the HTML panel leads with all survive as JSON."""
        source = NpsPanelSource()
        self._cache(
            source,
            {
                "full_name": "Gateway National Recreation Area",
                "description": "A park.",
                "url": "https://www.nps.gov/gate/",
                "designation": "National Recreation Area",
                "images": [{"url": "https://example.test/park.jpg"}],
                "activities": [{"name": "Hiking"}, {"name": "Astronomy"}],
            },
        )
        payload = source.api_payload(self.pin)
        assert payload is not None
        card = payload[PanelApiKind.INFO.value]
        self.assertEqual(card["heading_name"], "Gateway National Recreation Area")
        self.assertEqual(card["image_url"], "https://example.test/park.jpg")
        self.assertEqual(card["description"], "A park.")
        self.assertEqual(card["chips"], ["Hiking", "Astronomy"])
        self.assertEqual(card["footer_link"], {"url": "https://www.nps.gov/gate/", "label": "View on NPS.gov"})

    def test_nps_activity_chips_are_capped(self) -> None:
        """Dozens of activity tags stop characterizing a place and become noise."""
        source = NpsPanelSource()
        self._cache(source, {"full_name": "Big Park", "activities": [{"name": f"Activity {index}"} for index in range(30)]})
        payload = source.api_payload(self.pin)
        assert payload is not None
        self.assertEqual(len(payload[PanelApiKind.INFO.value]["chips"]), 8)

    def test_nominatim_coordinate_only_result_yields_none(self) -> None:
        """A geocode that found only an address tells the viewer nothing new."""
        source = NominatimPanelSource()
        self._cache(source, {"name": "", "osm_url": "https://osm.example/way/1"})
        self.assertIsNone(source.api_payload(self.pin))

    def test_nominatim_card_carries_contact_facts_and_wiki_links(self) -> None:
        """The quick-facts row and the wiki chips both become structured JSON."""
        source = NominatimPanelSource()
        self._cache(
            source,
            {
                "name": "Old Mill",
                "kind_label": "Historic building",
                "website": "https://mill.example",
                "phone": "+1 555 0100",
                "wikipedia": "de:Alte Mühle",
                "wikidata": "Q42",
                "osm_url": "https://osm.example/way/1",
            },
        )
        payload = source.api_payload(self.pin)
        assert payload is not None
        card = payload[PanelApiKind.INFO.value]
        self.assertEqual(card["chips"], ["Historic building"])
        self.assertEqual(card["facts"][0], {"icon": "language", "text": "https://mill.example", "href": "https://mill.example"})
        self.assertEqual(card["facts"][1]["href"], "tel:+1 555 0100")
        hrefs = {row["label"]: row["href"] for row in card["meta"]}
        self.assertEqual(hrefs["Wikipedia"], "https://de.wikipedia.org/wiki/Alte Mühle")
        self.assertEqual(hrefs["Wikidata"], "https://www.wikidata.org/wiki/Q42")

    def test_azure_maps_coordinate_only_result_yields_none(self) -> None:
        """Neither an address nor a POI means the payload is the input echoed back."""
        source = AzureMapsPanelSource()
        self._cache(source, {"poi": None})
        self.assertIsNone(source.api_payload(self.pin))

    def test_azure_maps_prefers_the_poi_name_over_the_address(self) -> None:
        """Matching the HTML panel: a named business beats a street address."""
        source = AzureMapsPanelSource()
        self._cache(
            source,
            {
                "formatted_address": "1 Main St, Springfield",
                "admin_district": "NY",
                "country": "United States",
                "poi": {"name": "Old Mill", "categories": ["landmark"], "phone": "+1 555 0100", "distance_meters": 12.4},
            },
        )
        payload = source.api_payload(self.pin)
        assert payload is not None
        card = payload[PanelApiKind.INFO.value]
        self.assertEqual(card["heading_name"], "Old Mill")
        self.assertEqual(card["chips"], ["landmark"])
        self.assertIn({"label": "Address", "value": "1 Main St, Springfield"}, card["meta"])
        self.assertIn("12m away", [fact["text"] for fact in card["facts"]])

    def test_usgs_no_maps_yields_none(self) -> None:
        """The web panel 204s on this; so does the API."""
        source = UsgsTopoPanelSource()
        self._cache(source, {"items": []})
        self.assertIsNone(source.api_payload(self.pin))

    def test_usgs_serves_a_summary_card_and_the_scans(self) -> None:
        """Both declared kinds are actually populated."""
        source = UsgsTopoPanelSource()
        self._cache(
            source,
            {
                "items": [
                    {"title": "Poughkeepsie, NY", "publicationDate": "1893-01-01", "downloadURL": "https://usgs.example/1.pdf", "previewGraphicURL": "https://usgs.example/1.jpg"},
                    {"title": "No download", "publicationDate": "1901-01-01"},
                ],
            },
        )
        payload = source.api_payload(self.pin)
        assert payload is not None
        self.assertEqual(payload[PanelApiKind.INFO.value]["chips"], ["2 maps"])
        media = payload[PanelApiKind.MEDIA.value]
        # The undownloadable second product is dropped - a media item whose url
        # goes nowhere is a broken tile, not a result.
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]["caption"], "Poughkeepsie, NY (1893)")
        self.assertEqual(media[0]["thumb_url"], "https://usgs.example/1.jpg")


class WikipediaTagUrlTests(SimpleTestCase):
    """OSM's ``wikipedia`` tag, which comes in two shapes and must not 404."""

    def test_language_prefixed_tag(self) -> None:
        """The conventional ``<lang>:<Title>`` form."""
        self.assertEqual(wikipedia_url("de:Kölner Dom"), "https://de.wikipedia.org/wiki/Kölner Dom")

    def test_bare_title_defaults_to_english(self) -> None:
        """Plenty of entries omit the prefix; English is the convention."""
        self.assertEqual(wikipedia_url("Packard Plant"), "https://en.wikipedia.org/wiki/Packard Plant")

    def test_empty_tag_yields_no_url(self) -> None:
        """No link beats a link to the Wikipedia main page."""
        self.assertEqual(wikipedia_url(""), "")


class InfoCardContractTests(SimpleTestCase):
    """``info_card`` is the one shape every INFO panel promises; hold it steady."""

    _KEYS = frozenset({"heading_name", "chips", "facts", "meta", "header_link", "footer_link", "image_url", "description"})

    @given(
        heading_name=st.one_of(st.none(), st.text()),
        chips=st.lists(st.text()),
        description=st.one_of(st.none(), st.text()),
    )
    def test_key_set_is_stable_for_any_input(self, heading_name: str | None, chips: list[str], description: str | None) -> None:
        """A consumer lays the card out once, so keys can't come and go.

        A missing key is indistinguishable from an older server to a client, so
        every field is always present even when the source never fills it.
        """
        card = info_card(heading_name=heading_name, chips=chips, description=description)
        self.assertEqual(set(card), self._KEYS)

    @given(chips=st.lists(st.one_of(st.text(), st.none())))
    def test_falsy_chips_are_dropped(self, chips: list[str | None]) -> None:
        """Sources build chip lists conditionally; empties must not become blank pills."""
        self.assertTrue(all(card_chip for card_chip in info_card(chips=chips)["chips"]))

    @given(heading_name=st.text(alphabet=" \t", max_size=5))
    def test_blank_scalars_normalize_to_none(self, heading_name: str) -> None:
        """Only truly empty strings normalize - whitespace is the source's business.

        Trimming here would quietly diverge from what the web panel renders for
        the same cached row, which is exactly the drift this contract exists to
        prevent.
        """
        card = info_card(heading_name=heading_name)
        self.assertEqual(card["heading_name"], heading_name or None)


class EpaFeatureGateTests(SimpleTestCase):
    """The subscription gate, declared on the source rather than only at a call site."""

    def test_nearby_facility_list_is_gated(self) -> None:
        """It is the "Nearby Research" tab's only member today."""
        self.assertEqual(EpaEchoNearbyPanelSource.required_feature, SiteFeature.NEARBY_RESEARCH)

    def test_exact_site_detail_is_not_gated(self) -> None:
        """The integration's primary purpose - an exact-site compliance card - is free."""
        self.assertIsNone(EpaEchoDetailPanelSource.required_feature)
