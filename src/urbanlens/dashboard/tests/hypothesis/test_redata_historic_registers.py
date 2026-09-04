"""The historic-register panel reaches REData's whole registry, not one inventory.

REData registers 25 historic inventories - the nationwide National Register plus
state SHPO layers and city/county registers - and UrbanLens read exactly one of
them, New York's CRIS, inside New York only. That was not curation: the CRIS
panel renders CRIS's own raw ArcGIS column names, so it *has* to name its
provider, and restricting the request left everything else unread.

What is worth testing here is not that a card renders. It is that the provider
list is arrived at from REData rather than written down, that the rows are read
from the fields REData standardizes rather than any one provider's, and that a
register with no display name still appears - the exact mistake that made
REData's `s2cloudless` invisible in the satellite carousel, where a name map was
doubling as a permission list.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.redata_historic_registers import (
    _REGISTER_LABELS,
    _SHOWN_ELSEWHERE,
    HistoricRegisterPanelSource,
    HistoricRegistersPlugin,
    register_label,
    register_rows,
)
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_cultural_resources_gateway"


def _resource(**overrides) -> dict:
    return {
        "provider": "md_mihp",
        "resource_type": "building",
        "scope": "structure",
        "name": "Hutzler Brothers Palace",
        "status": "Listed",
        "year_built": 1888,
        "architectural_style": "Romanesque Revival",
        "use_type": "Commercial",
        **overrides,
    }


class ProviderDiscoveryTests(TestCase):
    """Which registers get asked, and how that list is decided."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.source = HistoricRegisterPanelSource()

    def test_the_registers_asked_come_from_redatas_capability_index(self) -> None:
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=["nps_nrhp", "md_mihp"]) as tags,
            mock.patch(
                f"{_GATEWAY}.RedataCulturalResourcesGateway.near_resources",
                return_value=LocationContextEnvelope(count=0, complete=True),
            ) as near,
        ):
            self.source.fetch_envelope(39.3, -76.6)

        tags.assert_called_once_with(39.3, -76.6)
        self.assertEqual(near.call_args.kwargs["provider"], ["nps_nrhp", "md_mihp"])

    def test_a_register_redata_added_yesterday_is_asked_without_a_code_change(self) -> None:
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=["some_register_added_yesterday"]),
            mock.patch(
                f"{_GATEWAY}.RedataCulturalResourcesGateway.near_resources",
                return_value=LocationContextEnvelope(count=0, complete=True),
            ) as near,
        ):
            self.source.fetch_envelope(39.3, -76.6)

        self.assertEqual(near.call_args.kwargs["provider"], ["some_register_added_yesterday"])

    def test_cris_is_left_to_its_own_panel(self) -> None:
        """Including it would show the same USN record twice, the second time vaguer."""
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=["ny_cris", "nps_nrhp"]),
            mock.patch(
                f"{_GATEWAY}.RedataCulturalResourcesGateway.near_resources",
                return_value=LocationContextEnvelope(count=0, complete=True),
            ) as near,
        ):
            self.source.fetch_envelope(42.7, -73.8)

        self.assertEqual(near.call_args.kwargs["provider"], ["nps_nrhp"])

    def test_failed_discovery_asks_nothing_rather_than_every_register(self) -> None:
        """A request naming no provider runs all 25 - the one outcome to avoid."""
        with (
            mock.patch(f"{_GATEWAY}.applicable_provider_tags", return_value=[]),
            mock.patch(f"{_GATEWAY}.RedataCulturalResourcesGateway.near_resources") as near,
        ):
            envelope = self.source.fetch_envelope(39.3, -76.6)

        near.assert_not_called()
        self.assertEqual(envelope.results, [])
        self.assertTrue(envelope.complete, "nothing covers this point is a settled answer, not an outage")

    def test_the_excluded_tag_names_an_urbanlens_panel(self) -> None:
        """`_SHOWN_ELSEWHERE` is about this app's UI, which is why it may be written down."""
        from urbanlens.dashboard.services.pins.external_data import panel_sources

        keys = set(panel_sources())
        for tag in _SHOWN_ELSEWHERE:
            with self.subTest(tag=tag):
                self.assertTrue(
                    any(tag.replace("ny_", "") in key or key in tag for key in keys),
                    f"{tag} is excluded as 'shown elsewhere' but no panel key matches it",
                )

    def test_only_the_standardized_fields_are_cached(self) -> None:
        """`attributes`/`detail_payload`/`geometry` are per-provider, large, or both."""
        stored = self.source.transform_rows(
            [
                {
                    **_resource(),
                    "attributes": {"USNNum": "x"},
                    "detail_payload": {"big": "blob"},
                    "geometry": {"type": "Polygon"},
                }
            ]
        )

        self.assertEqual(
            set(stored[0]),
            {"provider", "resource_type", "scope", "name", "status", "year_built", "architectural_style", "use_type"},
        )


class RegisterLabelTests(SimpleTestCase):
    def test_a_known_register_gets_its_written_name(self) -> None:
        self.assertEqual(register_label("nps_nrhp"), "National Register of Historic Places")

    def test_an_unknown_register_is_readable_rather_than_dropped(self) -> None:
        """The name map must never act as a permission list."""
        self.assertEqual(register_label("some_new_register"), "Some New Register")

    def test_every_written_name_belongs_to_a_real_provider_tag(self) -> None:
        """A stale entry is harmless, but a typo hides a real register's name forever."""
        for tag in _REGISTER_LABELS:
            with self.subTest(tag=tag):
                self.assertEqual(tag, tag.lower().strip())
                self.assertNotIn(" ", tag)


class RegisterRowTests(SimpleTestCase):
    """Only the promoted fields every provider answers - never `attributes`."""

    def test_a_row_carries_its_register_and_what_the_record_says(self) -> None:
        rows = register_rows([_resource()])

        self.assertEqual(rows[0]["register"], "Maryland MIHP")
        self.assertEqual(rows[0]["name"], "Hutzler Brothers Palace")
        self.assertEqual(rows[0]["detail"], "1888, Commercial, Romanesque Revival, Listed")

    def test_a_sparse_row_still_renders(self) -> None:
        """Most registers populate none of year/style/use - only MHC does, today."""
        rows = register_rows([_resource(year_built=None, architectural_style=None, use_type=None, status="")])

        self.assertEqual(rows[0]["detail"], "")
        self.assertEqual(rows[0]["name"], "Hutzler Brothers Palace")

    def test_an_unnamed_row_is_dropped(self) -> None:
        self.assertEqual(register_rows([_resource(name="")]), [])

    def test_an_archaeological_buffer_is_not_a_description_of_anything(self) -> None:
        """REData publishes only an OBJECTID and a geometry for these, deliberately."""
        self.assertEqual(register_rows([_resource(resource_type="archaeological_buffer_area", name="Area 3")]), [])

    def test_malformed_rows_do_not_raise(self) -> None:
        self.assertEqual(register_rows(["not a dict", None, 7]), [])

    def test_order_is_preserved(self) -> None:
        rows = register_rows([_resource(name="A"), _resource(name="B"), _resource(name="C")])

        self.assertEqual([row["name"] for row in rows], ["A", "B", "C"])


class RenderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=39.3, longitude=-76.6)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)
        self.source = HistoricRegisterPanelSource()

    def test_records_are_grouped_by_register_in_the_chips(self) -> None:
        data = {
            "resources": [
                _resource(provider="md_mihp", name="A"),
                _resource(provider="md_mihp", name="B"),
                _resource(provider="nps_nrhp", name="C"),
            ],
        }

        context = self.source.render_context(self.pin, data)

        assert context is not None
        self.assertEqual(context["chips"], ["Maryland MIHP (2)", "National Register of Historic Places"])

    def test_each_row_names_its_register_and_the_record(self) -> None:
        context = self.source.render_context(self.pin, {"resources": [_resource()]})

        assert context is not None
        self.assertEqual(
            context["meta"][0],
            {
                "label": "Maryland MIHP",
                "value": "Hutzler Brothers Palace - 1888, Commercial, Romanesque Revival, Listed",
            },
        )

    def test_nothing_found_renders_nothing(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"resources": []}))

    def test_a_surveyed_campus_is_truncated_rather_than_listed_whole(self) -> None:
        data = {"resources": [_resource(name=f"Building {index}") for index in range(40)]}

        context = self.source.render_context(self.pin, data)

        assert context is not None
        self.assertEqual(len(context["meta"]), 10)
        self.assertEqual(context["chips"], ["Maryland MIHP (40)"], "the count still reports everything found")

    def test_an_empty_payload_has_no_tab(self) -> None:
        self.assertFalse(self.source.has_content({"resources": []}))
        self.assertTrue(self.source.has_content({"resources": [_resource()]}))


class SiteScopeTests(TestCase):
    """A parcel pin is the whole property; one surveyed outbuilding is not it."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=39.3, longitude=-76.6)
        self.pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)
        self.source = HistoricRegisterPanelSource()
        self.data = {
            "resources": [
                _resource(name="Tool House", scope="structure"),
                _resource(name="Mount Royal Historic District", scope="site", resource_type="building_district"),
            ],
        }

    def test_a_parcel_pin_leads_with_the_site_level_record(self) -> None:
        with mock.patch("urbanlens.dashboard.services.locations.site_scope.is_site_scope", return_value=True):
            context = self.source.render_context(self.pin, self.data)

        assert context is not None
        self.assertEqual(context["meta"][0]["value"].split(" - ")[0], "Mount Royal Historic District")

    def test_structure_records_are_ordered_after_rather_than_dropped(self) -> None:
        """A campus whose only records are its buildings must still show them."""
        with mock.patch("urbanlens.dashboard.services.locations.site_scope.is_site_scope", return_value=True):
            context = self.source.render_context(self.pin, self.data)

        assert context is not None
        self.assertEqual(len(context["meta"]), 2)

    def test_a_structure_pin_keeps_redatas_own_order(self) -> None:
        """Nearest-first, which is what a pin on one building wants."""
        with mock.patch("urbanlens.dashboard.services.locations.site_scope.is_site_scope", return_value=False):
            context = self.source.render_context(self.pin, self.data)

        assert context is not None
        self.assertEqual(context["meta"][0]["value"].split(" - ")[0], "Tool House")


class PluginContributionTests(SimpleTestCase):
    def test_the_plugin_contributes_the_panel(self) -> None:
        sources = HistoricRegistersPlugin().get_panel_sources()

        self.assertEqual([type(source).__name__ for source in sources], ["HistoricRegisterPanelSource"])

    def test_the_plugin_registers_its_rate_limit_budget(self) -> None:
        """A gateway with no registered defaults is unbudgeted, not free."""
        self.assertIn("redata_cultural_resources", HistoricRegistersPlugin().get_service_defaults())
