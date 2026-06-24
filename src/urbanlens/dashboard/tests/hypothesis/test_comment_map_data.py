"""Tests for comment map_data sanitization in the comments controller."""

from __future__ import annotations

from unittest.mock import MagicMock

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.comments import (
    _parse_map_data,
    _sanitize_markup_color,
    _sanitize_markup_shapes,
    _sanitize_number,
)


def _request(map_data_json: str) -> MagicMock:
    req = MagicMock()
    req.POST.get.side_effect = lambda key, default="": map_data_json if key == "map_data" else default
    return req


class CommentMapDataSanitizationTests(TestCase):
    """_parse_map_data strips dangerous color/style values before storage."""

    def test_malicious_hex_injection_in_color_is_rejected(self) -> None:
        import json
        payload = json.dumps({
            "center_lat": 41.0,
            "center_lng": -74.0,
            "shapes": [{"type": "line", "latlngs": [[41.0, -74.0], [41.1, -74.1]], "color": '"><script>alert(1)</script>'}],
        })
        result = _parse_map_data(_request(payload))
        assert result is not None
        shape = result["shapes"][0]
        assert shape["color"] == "#e74c3c", f"Expected fallback hex, got {shape['color']!r}"

    def test_css_expression_in_color_is_rejected(self) -> None:
        import json
        payload = json.dumps({
            "center_lat": 41.0,
            "center_lng": -74.0,
            "shapes": [{"type": "polygon", "latlngs": [[41.0, -74.0], [41.1, -74.1], [41.0, -74.1]], "color": "red;expression(alert(1))"}],
        })
        result = _parse_map_data(_request(payload))
        assert result is not None
        assert result["shapes"][0]["color"] == "#e74c3c"

    def test_invalid_center_coordinates_are_rejected(self) -> None:
        import json
        for bad_lat, bad_lng in [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0), ("x", 0.0)]:
            payload = json.dumps({"center_lat": bad_lat, "center_lng": bad_lng, "shapes": []})
            assert _parse_map_data(_request(payload)) is None, f"Expected None for lat={bad_lat} lng={bad_lng}"

    def test_valid_hex_color_is_preserved(self) -> None:
        import json
        payload = json.dumps({
            "center_lat": 51.5, "center_lng": -0.1,
            "shapes": [{"type": "rect", "latlngs": [[51.5, -0.1], [51.6, 0.0]], "color": "#2196F3"}],
        })
        result = _parse_map_data(_request(payload))
        assert result is not None
        assert result["shapes"][0]["color"] == "#2196F3"

    def test_unknown_shape_type_is_dropped(self) -> None:
        import json
        payload = json.dumps({
            "center_lat": 0.0, "center_lng": 0.0,
            "shapes": [{"type": "__proto__", "latlngs": [[0.0, 0.0], [1.0, 1.0]], "color": "#ffffff"}],
        })
        result = _parse_map_data(_request(payload))
        assert result is not None
        assert result["shapes"] == []

    def test_out_of_range_latlng_pairs_are_stripped(self) -> None:
        import json
        payload = json.dumps({
            "center_lat": 0.0, "center_lng": 0.0,
            "shapes": [{"type": "line", "latlngs": [[200.0, 0.0], [0.0, 0.0], [1.0, 1.0]], "color": "#ff0000"}],
        })
        result = _parse_map_data(_request(payload))
        assert result is not None
        assert len(result["shapes"][0]["latlngs"]) == 2

    def test_stroke_width_is_clamped(self) -> None:
        import json
        payload = json.dumps({
            "center_lat": 0.0, "center_lng": 0.0,
            "shapes": [{"type": "line", "latlngs": [[0.0, 0.0], [1.0, 1.0]], "color": "#ff0000", "stroke_width": 9999}],
        })
        result = _parse_map_data(_request(payload))
        assert result is not None
        assert result["shapes"][0]["stroke_width"] <= 50

    def test_zoom_is_clamped(self) -> None:
        import json
        payload = json.dumps({"center_lat": 0.0, "center_lng": 0.0, "zoom": 999, "shapes": []})
        result = _parse_map_data(_request(payload))
        assert result is not None
        assert result["zoom"] <= 22


class SanitizeColorUnitTests(TestCase):
    """Unit tests for _sanitize_markup_color."""

    def test_valid_hex_passes(self) -> None:
        assert _sanitize_markup_color("#aabbcc") == "#aabbcc"
        assert _sanitize_markup_color("#FFFFFF") == "#FFFFFF"

    def test_invalid_values_return_fallback(self) -> None:
        for bad in ["red", "rgb(1,2,3)", "", None, 42, "#gg0000", "#fff"]:
            assert _sanitize_markup_color(bad) == "#e74c3c", f"Expected fallback for {bad!r}"

    def test_custom_fallback_used(self) -> None:
        assert _sanitize_markup_color("bad", "#000000") == "#000000"


class SanitizeNumberUnitTests(TestCase):
    """Unit tests for _sanitize_number."""

    def test_clamps_above_hi(self) -> None:
        self.assertEqual(_sanitize_number(999, 0, 100, 50), 100.0)

    def test_clamps_below_lo(self) -> None:
        self.assertEqual(_sanitize_number(-1, 0, 100, 50), 0.0)

    def test_non_numeric_returns_default(self) -> None:
        self.assertEqual(_sanitize_number("bad", 0, 100, 42), 42.0)

    def test_valid_value_passes_through(self) -> None:
        self.assertEqual(_sanitize_number(75, 0, 100, 50), 75.0)
