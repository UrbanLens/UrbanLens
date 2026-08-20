"""The site-features panel reaches REData's registry without hardcoding it.

REData's points-of-interest registry holds about two dozen providers and
UrbanLens reached two. The rest are the ones closest to this app's subject:
agency surveillance-camera registers, OpenStreetMap's worldwide contributed
camera set, FCC-registered antenna structures, FAA facility groups, EPA
contamination programmes, storage tanks.

The thing worth testing is not that a panel renders - it is *how the provider
list is arrived at*. Most of these providers are generated on REData's side from
dataset tables, so a list written into UrbanLens would stop growing silently.
These tests pin the two properties that keep that from happening: the panel asks
REData which providers cover the point, and the only tags it names itself are
ones that identify an **UrbanLens panel**, not a REData source.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.redata_site_features import _SHOWN_ELSEWHERE, _TOO_GENERIC, SiteFeaturesPanelSource, SiteFeaturesPlugin, feature_rows
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway"


def _camera(name: str = "Main St & 1st Ave", category: str = "Red-light camera") -> dict:
    return {"provider": "chicago_red_light_cameras", "name": name, "category": category, "url": "https://example.test/cam/1", "latitude": 41.9, "longitude": -87.6, "attributes": {"agency": "CDOT"}}


class ProviderDiscoveryTests(TestCase):
    """Which providers get asked, and how that list is decided."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.source = SiteFeaturesPanelSource()

    def test_the_providers_asked_come_from_redatas_capability_index(self) -> None:
        """Not from a list in this repo - see the module docstring for why."""
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=["chicago_red_light_cameras", "fcc_asr", "osm_surveillance"]) as tags,
            mock.patch(f"{_GATEWAY}.RedataPointsOfInterestGateway.near_point", return_value=LocationContextEnvelope(count=0, complete=True)) as near,
        ):
            self.source.fetch_envelope(41.9, -87.6)

        tags.assert_called_once_with(41.9, -87.6)
        self.assertEqual(near.call_args.kwargs["provider"], ["chicago_red_light_cameras", "fcc_asr", "osm_surveillance"])

    def test_providers_with_their_own_panel_are_left_out(self) -> None:
        """Including them would show the same facility twice under a vaguer heading."""
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=["fcc_asr", "yelp", "epa_echo", "nps_places", "osm"]),
            mock.patch(f"{_GATEWAY}.RedataPointsOfInterestGateway.near_point", return_value=LocationContextEnvelope(count=0, complete=True)) as near,
        ):
            self.source.fetch_envelope(41.9, -87.6)

        self.assertEqual(near.call_args.kwargs["provider"], ["fcc_asr"])

    def test_a_new_redata_provider_is_asked_without_a_code_change(self) -> None:
        """The property the capability lookup exists for."""
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=["some_register_redata_added_yesterday"]),
            mock.patch(f"{_GATEWAY}.RedataPointsOfInterestGateway.near_point", return_value=LocationContextEnvelope(count=0, complete=True)) as near,
        ):
            self.source.fetch_envelope(41.9, -87.6)

        self.assertEqual(near.call_args.kwargs["provider"], ["some_register_redata_added_yesterday"])

    def test_failed_discovery_asks_nothing_rather_than_everything(self) -> None:
        """A request with no `provider` fans out across the whole registry.

        That is the one outcome the capability lookup exists to prevent, so a
        discovery failure must not fall through to it.
        """
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=[]),
            mock.patch(f"{_GATEWAY}.RedataPointsOfInterestGateway.near_point") as near,
        ):
            envelope = self.source.fetch_envelope(41.9, -87.6)

        near.assert_not_called()
        self.assertEqual(envelope.results, [])
        self.assertTrue(envelope.complete, "nothing covers this point is a settled answer, not an outage")

    def test_the_two_exclusion_lists_stay_separate(self) -> None:
        """A tag in both would make the panel-backed check vacuous for it."""
        self.assertEqual(_SHOWN_ELSEWHERE & _TOO_GENERIC, frozenset())

    def test_openstreetmaps_camera_data_is_not_excluded_with_its_generic_points(self) -> None:
        """`osm` is dropped for noise; `osm_surveillance` is the reason this panel exists."""
        self.assertIn("osm", _TOO_GENERIC)
        self.assertNotIn("osm_surveillance", _TOO_GENERIC | _SHOWN_ELSEWHERE)

    def test_the_excluded_tags_all_name_an_urbanlens_panel(self) -> None:
        """`_SHOWN_ELSEWHERE` is about this app's UI, which is why it may be written down.

        If a tag here stopped matching a panel, the list would have quietly
        become a REData-taxonomy list - the kind that goes stale. Judgements
        about REData's taxonomy live in `_TOO_GENERIC` instead, where the fact
        that they can go stale is stated rather than hidden among these.
        """
        from urbanlens.dashboard.services.pins.external_data import panel_sources

        keys = set(panel_sources())
        for tag in _SHOWN_ELSEWHERE:
            with self.subTest(tag=tag):
                self.assertTrue(
                    any(key == tag or key.startswith(f"{tag}_") or tag.startswith(key) for key in keys),
                    f"{tag} is excluded as 'shown elsewhere' but no panel key matches it",
                )


class FeatureRowTests(SimpleTestCase):
    """Only the promoted fields every provider answers - never `attributes`."""

    def test_a_row_is_labelled_by_redatas_own_category(self) -> None:
        rows = feature_rows([_camera()])

        self.assertEqual(rows, [{"category": "Red-light camera", "name": "Main St & 1st Ave", "url": "https://example.test/cam/1"}])

    def test_a_row_with_no_name_falls_back_to_its_description(self) -> None:
        rows = feature_rows([{"category": "Antenna structure", "description": "Guyed mast, 120m", "url": ""}])

        self.assertEqual(rows[0]["name"], "Guyed mast, 120m")

    def test_a_row_with_neither_name_nor_category_is_dropped(self) -> None:
        self.assertEqual(feature_rows([{"url": "https://example.test"}]), [])

    def test_malformed_rows_do_not_raise(self) -> None:
        self.assertEqual(feature_rows(["not a dict", None, 7]), [])

    def test_order_is_preserved(self) -> None:
        """REData returns nearest-first; re-sorting would discard that."""
        rows = feature_rows([_camera("A"), _camera("B"), _camera("C")])

        self.assertEqual([row["name"] for row in rows], ["A", "B", "C"])


class RenderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.9, longitude=-87.6)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)
        self.source = SiteFeaturesPanelSource()

    def test_features_are_grouped_by_category_in_the_chips(self) -> None:
        data = {
            "features": [
                _camera("A", "Red-light camera"),
                _camera("B", "Red-light camera"),
                _camera("C", "Speed camera"),
                {"category": "Antenna structure", "name": "Mast", "url": ""},
            ],
        }

        context = self.source.render_context(self.pin, data)

        assert context is not None
        self.assertEqual(context["chips"], ["2 red-light cameras", "1 antenna structure", "1 speed camera"])

    def test_each_row_is_labelled_and_linked(self) -> None:
        context = self.source.render_context(self.pin, {"features": [_camera()]})

        assert context is not None
        self.assertEqual(context["meta"][0], {"label": "Red-light camera", "value": "Main St & 1st Ave", "href": "https://example.test/cam/1"})

    def test_nothing_found_renders_nothing(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"features": []}))

    def test_a_dense_block_is_truncated_rather_than_listed_whole(self) -> None:
        data = {"features": [_camera(f"Camera {index}") for index in range(40)]}

        context = self.source.render_context(self.pin, data)

        assert context is not None
        self.assertEqual(len(context["meta"]), 10)
        self.assertEqual(context["chips"], ["40 red-light cameras"], "the count still reports everything found")

    def test_an_empty_payload_has_no_tab(self) -> None:
        """`inspects_content`: a successful fetch that found nothing is not a tab."""
        self.assertFalse(self.source.has_content({"features": []}))
        self.assertTrue(self.source.has_content({"features": [_camera()]}))


class PluginContributionTests(SimpleTestCase):
    def test_the_plugin_contributes_the_panel(self) -> None:
        sources = SiteFeaturesPlugin().get_panel_sources()

        self.assertEqual([type(source).__name__ for source in sources], ["SiteFeaturesPanelSource"])
