"""Property tests for the pin export writers (UL-377/UL-382).

Pure-logic tests over a lightweight duck-typed stand-in for ``Pin`` (name,
coordinates, description) rather than real model instances, per this
project's "@given and self.client don't mix" rule - these writers never
touch the DB, so there's no reason to pay for one.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
import math

from fastkml import kml
import gpxpy
from hypothesis import given, strategies as st
from pygeoif.geometry import Point

from urbanlens.dashboard.services.export_formats import pins_to_csv, pins_to_geojson, pins_to_gpx, pins_to_kml


@dataclass
class _FakePin:
    """Stand-in exposing the same attributes the writers actually read off Pin."""

    effective_name: str
    effective_latitude: float
    effective_longitude: float
    description: str | None


_printable_text = st.text(alphabet=st.characters(whitelist_categories=("L", "N", "P", "Zs")), max_size=50)
_latitudes = st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False)
_longitudes = st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False)
_names = _printable_text.filter(lambda s: s.strip())
_descriptions = _printable_text

_pins = st.builds(_FakePin, effective_name=_names, effective_latitude=_latitudes, effective_longitude=_longitudes, description=_descriptions)


@given(st.lists(_pins, max_size=10))
def test_geojson_round_trips_feature_count_and_coordinate_order(pins: list[_FakePin]) -> None:
    data = json.loads(pins_to_geojson(pins))
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == len(pins)
    for pin, feature in zip(pins, data["features"], strict=True):
        assert feature["geometry"]["coordinates"] == [pin.effective_longitude, pin.effective_latitude]
        assert feature["properties"]["name"] == pin.effective_name


@given(st.lists(_pins, max_size=10))
def test_kml_round_trips_placemark_count_and_coordinates(pins: list[_FakePin]) -> None:
    output = pins_to_kml(pins)
    parsed = kml.KML.from_string(output)
    document = next(iter(parsed.features))
    assert isinstance(document, kml.Document)
    placemarks = list(document.features)
    assert len(placemarks) == len(pins)
    for pin, placemark in zip(pins, placemarks, strict=True):
        assert isinstance(placemark, kml.Placemark)
        # lxml's prettyprint reformats a name's leading/trailing whitespace on
        # serialization - immaterial for real place names, so compare stripped.
        assert (placemark.name or "").strip() == pin.effective_name.strip()
        geometry = placemark.geometry
        assert isinstance(geometry, Point)
        assert geometry.x == pin.effective_longitude
        assert geometry.y == pin.effective_latitude


@given(st.lists(_pins, max_size=10))
def test_gpx_round_trips_waypoint_count_and_coordinates(pins: list[_FakePin]) -> None:
    parsed = gpxpy.parse(pins_to_gpx(pins))
    assert len(parsed.waypoints) == len(pins)
    for pin, waypoint in zip(pins, parsed.waypoints, strict=True):
        assert waypoint.name == pin.effective_name
        # GPX serializes coordinates to 6 decimal places (~11cm precision) -
        # not a bug, just the format's standard precision budget.
        assert math.isclose(waypoint.latitude, pin.effective_latitude, abs_tol=1e-6)
        assert math.isclose(waypoint.longitude, pin.effective_longitude, abs_tol=1e-6)


@given(st.lists(_pins, max_size=10))
def test_csv_round_trips_row_count_and_values(pins: list[_FakePin]) -> None:
    rows = list(csv.reader(io.StringIO(pins_to_csv(pins))))
    header, *data_rows = rows
    assert header == ["name", "latitude", "longitude", "description"]
    assert len(data_rows) == len(pins)
    for pin, row in zip(pins, data_rows, strict=True):
        assert row[0] == pin.effective_name
        assert float(row[1]) == pin.effective_latitude
        assert float(row[2]) == pin.effective_longitude
        assert row[3] == pin.description


def test_empty_pin_list_produces_valid_empty_documents() -> None:
    assert json.loads(pins_to_geojson([]))["features"] == []
    assert gpxpy.parse(pins_to_gpx([])).waypoints == []
    assert list(csv.reader(io.StringIO(pins_to_csv([]))))[1:] == []
    parsed = kml.KML.from_string(pins_to_kml([]))
    document = next(iter(parsed.features))
    assert isinstance(document, kml.Document)
    assert list(document.features) == []
