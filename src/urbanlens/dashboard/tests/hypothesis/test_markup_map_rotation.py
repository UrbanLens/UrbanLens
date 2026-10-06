"""A map saved turned, a label saved turned, and the credits for the area an Esri basemap shows.

The composer can turn the map and each text label. Both are saved as degrees: the map's ``bearing``
is the compass direction that points up (MapLibre's sense), and a label's ``rotation`` is clockwise
on screen. Neither may be lost on the way through the sanitizer, the model or an archive, and an old
snapshot without them is north-up and level.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.services.core.numbers import degrees_or_none
from urbanlens.dashboard.services.map.map_snapshot import sanitize_map_data

_BASE = {"center_lat": 42.65, "center_lng": -73.75, "zoom": 15}
_TEXT = {"type": "text", "latlngs": [[42.65, -73.75]], "color": "#000000", "stroke_width": 16, "label": "Gate"}


class BearingSanitizeTests(SimpleTestCase):
    """``sanitize_map_data`` keeps a bearing as an angle in [0, 360), and nothing else."""

    def test_a_snapshot_without_a_bearing_is_north_up(self) -> None:
        result = sanitize_map_data(dict(_BASE))
        assert result is not None  # nosec B101
        self.assertEqual(result["bearing"], 0.0)

    def test_a_bearing_in_range_is_kept(self) -> None:
        result = sanitize_map_data({**_BASE, "bearing": 315})
        assert result is not None  # nosec B101
        self.assertEqual(result["bearing"], 315.0)

    def test_a_bearing_past_a_full_turn_wraps_rather_than_clamps(self) -> None:
        for raw, expected in ((370, 10.0), (-350, 10.0), (-90, 270.0), (720, 0.0)):
            result = sanitize_map_data({**_BASE, "bearing": raw})
            assert result is not None  # nosec B101
            self.assertAlmostEqual(result["bearing"], expected, msg=f"bearing {raw}")

    def test_anything_not_a_finite_number_is_north_up(self) -> None:
        for raw in (True, False, "90", None, [], {}, math.nan, math.inf, -math.inf):
            result = sanitize_map_data({**_BASE, "bearing": raw})
            assert result is not None  # nosec B101
            self.assertEqual(result["bearing"], 0.0, f"bearing {raw!r}")

    def test_a_negative_bearing_too_small_to_register_is_north_not_360(self) -> None:
        """Found by the property below: ``-1e-141 % 360.0`` is exactly ``360.0`` in floating point."""
        self.assertEqual(degrees_or_none(-1.2776693295072146e-141), 0.0)
        self.assertEqual(degrees_or_none(-0.0), 0.0)

    def test_an_integer_too_big_for_a_float_is_not_an_angle(self) -> None:
        self.assertIsNone(degrees_or_none(10**400))

    @settings(max_examples=50, deadline=None)
    @given(st.floats(allow_nan=True, allow_infinity=True) | st.integers(min_value=-(10**400), max_value=10**400))
    def test_any_number_lands_in_range(self, raw: float) -> None:
        result = sanitize_map_data({**_BASE, "bearing": raw})
        assert result is not None  # nosec B101
        self.assertGreaterEqual(result["bearing"], 0.0)
        self.assertLess(result["bearing"], 360.0)


class TextRotationSanitizeTests(SimpleTestCase):
    """A label's turn survives the sanitizer; other shapes never carry one."""

    def _markup(self, *shapes: dict) -> list[dict]:
        result = sanitize_map_data({**_BASE, "markup": list(shapes)})
        assert result is not None  # nosec B101
        return result["markup"]

    def test_a_turned_label_keeps_its_turn(self) -> None:
        self.assertEqual(self._markup({**_TEXT, "rotation": 30})[0]["rotation"], 30.0)

    def test_an_anticlockwise_turn_is_kept_as_the_same_angle(self) -> None:
        self.assertEqual(self._markup({**_TEXT, "rotation": -30})[0]["rotation"], 330.0)

    def test_a_level_label_carries_no_rotation(self) -> None:
        for raw in (0, 360, None, "45", True, math.nan):
            self.assertNotIn("rotation", self._markup({**_TEXT, "rotation": raw})[0], f"rotation {raw!r}")
        self.assertNotIn("rotation", self._markup(_TEXT)[0])

    def test_a_line_is_never_given_a_rotation(self) -> None:
        line = {"type": "line", "latlngs": [[42.65, -73.75], [42.66, -73.74]], "rotation": 45}
        self.assertNotIn("rotation", self._markup(line)[0])


class TextRotationModelTests(SimpleTestCase):
    """``PinMarkup`` stores a label's turn in its geometry and gives it back."""

    def test_a_turned_label_round_trips(self) -> None:
        item = PinMarkup.from_snapshot_shape({**_TEXT, "rotation": 330.0})
        assert item is not None  # nosec B101
        self.assertEqual(item.geometry["rotation"], 330.0)
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        self.assertEqual(result["rotation"], 330.0)
        self.assertEqual(result["label"], "Gate")

    def test_a_level_label_stores_no_rotation(self) -> None:
        for raw in (0, 0.0, True, "30", None):
            item = PinMarkup.from_snapshot_shape({**_TEXT, "rotation": raw})
            assert item is not None  # nosec B101
            self.assertNotIn("rotation", item.geometry, f"rotation {raw!r}")
            result = item.to_snapshot_shape()
            assert result is not None  # nosec B101
            self.assertNotIn("rotation", result)

    def test_a_label_saved_before_labels_turned_loads_level(self) -> None:
        item = PinMarkup(markup_type="text", geometry={"type": "Point", "coordinates": [-73.75, 42.65]}, label="Old")
        result = item.to_snapshot_shape()
        assert result is not None  # nosec B101
        self.assertNotIn("rotation", result)
        self.assertEqual(result["latlngs"], [[42.65, -73.75]])


class BearingModelTests(TestCase):
    """``MarkupMap`` keeps the bearing through a save and gives it back in its snapshot."""

    def setUp(self) -> None:
        self.profile = baker.make("auth.User").profile

    def test_a_new_map_is_north_up(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        self.assertEqual(markup_map.bearing, 0.0)
        self.assertEqual(markup_map.to_snapshot()["bearing"], 0.0)

    def test_a_turned_map_round_trips_through_its_snapshot(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile)
        snapshot = sanitize_map_data({**_BASE, "bearing": 315, "markup": [{**_TEXT, "rotation": 30}]})
        assert snapshot is not None  # nosec B101
        markup_map.replace_items_from_snapshot(snapshot)
        markup_map.refresh_from_db()
        self.assertEqual(markup_map.bearing, 315.0)
        result = markup_map.to_snapshot()
        self.assertEqual(result["bearing"], 315.0)
        self.assertEqual(result["markup"][0]["rotation"], 30.0)

    def test_saving_a_snapshot_without_a_bearing_turns_the_map_back_north(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.profile, bearing=90.0)
        markup_map.replace_items_from_snapshot({**_BASE, "markup": []})
        markup_map.refresh_from_db()
        self.assertEqual(markup_map.bearing, 0.0)

    def test_a_copy_of_a_turned_map_is_turned(self) -> None:
        """Sharing a map clones it through ``to_snapshot``/``replace_items_from_snapshot``."""
        source = MarkupMap.objects.create(profile=self.profile, bearing=45.0)
        copy = MarkupMap.objects.create(profile=self.profile)
        copy.replace_items_from_snapshot(source.to_snapshot())
        self.assertEqual(copy.bearing, 45.0)


class BearingEndpointTests(TestCase):
    """The create and view-state endpoints take a bearing, and refuse what is not one."""

    def setUp(self) -> None:
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)

    def _post(self, name: str, body: dict, *args: str):
        return self.client.post(reverse(name, args=args), data=json.dumps(body), content_type="application/json")

    def test_a_one_shot_save_keeps_the_bearing(self) -> None:
        response = self._post("markup_map.create", {**_BASE, "bearing": 315, "markup": [{**_TEXT, "rotation": 30}]})
        self.assertEqual(response.status_code, 200)
        markup_map = MarkupMap.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(markup_map.bearing, 315.0)
        snapshot = self.client.get(reverse("markup_map.snapshot", args=[markup_map.uuid])).json()
        self.assertEqual(snapshot["bearing"], 315.0)
        self.assertEqual(snapshot["markup"][0]["rotation"], 30.0)

    def test_view_state_sets_and_wraps_the_bearing(self) -> None:
        map_uuid = self._post("markup_map.create", dict(_BASE)).json()["uuid"]
        self.assertEqual(self._post("markup_map.view_state", {"bearing": -45}, map_uuid).status_code, 200)
        self.assertEqual(MarkupMap.objects.get(uuid=map_uuid).bearing, 315.0)

    def test_view_state_ignores_a_bearing_that_is_not_a_number(self) -> None:
        map_uuid = self._post("markup_map.create", {**_BASE, "bearing": 90}).json()["uuid"]
        self.assertEqual(MarkupMap.objects.get(uuid=map_uuid).bearing, 90.0)
        for raw in (True, "180", None, [1]):
            self._post("markup_map.view_state", {"bearing": raw}, map_uuid)
            self.assertEqual(MarkupMap.objects.get(uuid=map_uuid).bearing, 90.0, f"bearing {raw!r}")


class EsriServiceTests(SimpleTestCase):
    """The proxied Esri layers name the service the browser asks for that area's credits."""

    def test_each_esri_layer_names_its_service(self) -> None:
        from urbanlens.dashboard.services.map.basemap_vendors import VENDOR_TILES

        self.assertEqual(VENDOR_TILES["satellite"].esri_service, "World_Imagery")
        self.assertEqual(VENDOR_TILES["terrain"].esri_service, "World_Topo_Map")
        self.assertEqual(VENDOR_TILES["street"].esri_service, "World_Street_Map")
        self.assertEqual(VENDOR_TILES["borders"].esri_service, "Reference/World_Boundaries_and_Places")

    def test_a_vendor_that_is_not_esri_names_none(self) -> None:
        from urbanlens.dashboard.services.map.basemap_vendors import VendorTiles

        self.assertIsNone(VendorTiles(url_template="https://tiles.example/{z}/{x}/{y}.png").esri_service)
        # A look-alike host is not Esri's.
        self.assertIsNone(
            VendorTiles(
                url_template="https://server.arcgisonline.com.evil.test/ArcGIS/rest/services/X/MapServer/tile/{z}/{y}/{x}"
            ).esri_service
        )

    def test_the_credits_endpoint_is_admitted_by_connect_src(self) -> None:
        from urbanlens.UrbanLens.settings.base import _CSP_DIRECTIVES

        source = (
            Path(__file__).resolve().parents[2] / "frontend" / "ts" / "shared" / "esri-attribution.ts"
        ).read_text()
        match = re.search(r'ATTRIBUTION_ROOT = "(https://[^/"]+)/', source)
        assert match is not None  # nosec B101
        self.assertIn(match.group(1), _CSP_DIRECTIVES["connect-src"])


class LeafletRotateConfigTests(SimpleTestCase):
    """The composer is told where to load leaflet-rotate from, with a hash that covers what it loads."""

    def test_the_config_names_leaflet_rotate(self) -> None:
        from urbanlens.dashboard.context_processors import add_comment_map_config
        from urbanlens.dashboard.services.core.vendor_assets import VENDOR_ASSETS

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        config = add_comment_map_config(request)["comment_map_config"]()
        asset = VENDOR_ASSETS["leaflet_rotate_js"]
        self.assertEqual(config["leafletRotate"], {"src": asset.fallback, "integrity": asset.integrity})

    def test_a_mirror_drops_the_hash_that_describes_the_cdn_copy(self) -> None:
        from urbanlens.dashboard.services.core.vendor_assets import vendor_asset_source

        with mock.patch(
            "urbanlens.dashboard.services.core.vendor_assets._mirror_root",
            return_value="https://assets.example.test/vendor",
        ):
            source = vendor_asset_source("leaflet_rotate_js")
        self.assertTrue(source["src"].startswith("https://assets.example.test/vendor/"))
        self.assertEqual(source["integrity"], "")
