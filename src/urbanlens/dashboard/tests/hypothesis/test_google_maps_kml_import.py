"""Tests for GoogleMapsGateway.takeout_kml_to_dict() - KML/KMZ pin extraction.

Regression coverage for two bugs that silently produced zero pins for every
real-world KML/KMZ import (Google Takeout always emits an XML encoding
declaration, and Google MyMaps always nests Placemarks inside a Folder):

- Passing a decoded ``str`` (rather than raw ``bytes``) to fastkml, which lxml
  rejects when the document has an ``<?xml ... encoding=...?>`` declaration.
- Reading ``k.features`` (a list attribute in fastkml>=1.4) as if it were a
  method, and assuming Placemarks sit exactly one level below the KML root.
"""
from __future__ import annotations

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

_hyp = hyp_settings(max_examples=40, deadline=None)

_KML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Test Trip</name>
    {body}
  </Document>
</kml>
"""

_PLACEMARK_TEMPLATE = """<Placemark>
  <name>{name}</name>
  <description>{description}</description>
  <Point><coordinates>{lon},{lat},0.0</coordinates></Point>
</Placemark>
"""


def _placemark_xml(name: str, lat: float, lon: float, description: str = "") -> str:
    return _PLACEMARK_TEMPLATE.format(name=name, lat=lat, lon=lon, description=description)


def _kml_bytes(body: str) -> bytes:
    return _KML_TEMPLATE.format(body=body).encode("utf-8")


class TakeoutKmlToDictTests(TestCase):
    """takeout_kml_to_dict() extracts pins regardless of encoding declaration or nesting depth."""

    def setUp(self):
        self.gateway = GoogleMapsGateway(api_key="test-key")
        self.profile = object()

    def test_placemark_directly_under_document(self):
        data = _kml_bytes(_placemark_xml("BYTE", 42.9013318, -73.3513978, "A ruin"))

        pins = self.gateway.takeout_kml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "BYTE")
        self.assertEqual(pins[0]["description"], "A ruin")
        self.assertAlmostEqual(pins[0]["latitude"], 42.9013318)
        self.assertAlmostEqual(pins[0]["longitude"], -73.3513978)
        self.assertIs(pins[0]["profile"], self.profile)

    def test_placemark_nested_in_folder(self):
        # Google MyMaps (and therefore every Google Takeout KMZ) wraps every
        # layer's placemarks in a <Folder>, one level deeper than a bare Document.
        body = f"<Folder><name>Layer</name>{_placemark_xml('Green Mountain', 42.75482, -73.234856)}</Folder>"
        data = _kml_bytes(body)

        pins = self.gateway.takeout_kml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "Green Mountain")

    def test_multiple_placemarks_across_mixed_nesting(self):
        body = (
            _placemark_xml("Top Level", 1.0, 2.0)
            + f"<Folder><name>Nested</name>{_placemark_xml('Nested Pin', 3.0, 4.0)}</Folder>"
        )
        data = _kml_bytes(body)

        pins = self.gateway.takeout_kml_to_dict(data, self.profile)

        self.assertEqual({p["name"] for p in pins}, {"Top Level", "Nested Pin"})

    def test_document_with_no_placemarks_returns_empty_list(self):
        data = _kml_bytes("")

        pins = self.gateway.takeout_kml_to_dict(data, self.profile)

        self.assertEqual(pins, [])

    def test_https_kml_namespace_still_parses(self):
        # Some third-party exporters (e.g. multiplottr.com) declare the KML
        # namespace as `https://www.opengis.net/kml/2.2` instead of `http://`.
        # fastkml matches elements by exact namespace URI, so this previously
        # produced zero features with no exception raised - a silent failure
        # surfaced to users as "No valid location files found in the upload."
        data = _kml_bytes(_placemark_xml("HTTPS Namespace", 38.6244206, -90.1610675)).replace(
            b"http://www.opengis.net/kml/2.2",
            b"https://www.opengis.net/kml/2.2",
        )

        pins = self.gateway.takeout_kml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], "HTTPS Namespace")

    @_hyp
    @given(
        name=st.text(
            alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="<>&\"'"),
            min_size=1,
            max_size=40,
        ).map(str.strip).filter(bool),
        lat=st.floats(min_value=-89, max_value=89, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False),
    )
    def test_round_trips_arbitrary_name_and_coordinates(self, name: str, lat: float, lon: float):
        data = _kml_bytes(_placemark_xml(name, lat, lon))

        pins = self.gateway.takeout_kml_to_dict(data, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]["name"], name)
        self.assertAlmostEqual(pins[0]["latitude"], lat, places=6)
        self.assertAlmostEqual(pins[0]["longitude"], lon, places=6)
