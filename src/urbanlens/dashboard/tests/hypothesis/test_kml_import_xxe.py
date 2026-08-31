"""KML import must reject XML entity attacks before fastkml sees the document.

``takeout_kml_to_dict`` hands raw upload bytes to ``fastkml``, which parses with
``lxml`` and no hardening of its own - the same gap the GPX importers already
close with a defusedxml pre-parse (``import_formats/gpx.py``). Without it a KML
upload is an XXE and entity-expansion surface: the payloads below read a local
file into a placemark name, and expand a few hundred bytes into gigabytes of
memory inside the process doing the parse.

The attacks are written as *rejections* rather than as assertions about what
gets read. "The file contents did not appear in the output" would pass against a
build where lxml simply had entity resolution off by default, and would keep
passing if that default ever changed. Refusing to parse a document that declares
a DTD at all is the property that actually holds.
"""

from __future__ import annotations

from django.test import SimpleTestCase
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile

#: Reads a local file into the placemark name. The classic XXE.
XXE_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE kml [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>&xxe;</name>
      <Point><coordinates>-73.9,40.7,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""

#: "Billion laughs" - nested entity expansion. No external reference at all, so
#: a defence that only blocks SYSTEM entities lets this one through.
BILLION_LAUGHS_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE kml [
  <!ENTITY a "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
  <!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
]>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>&e;</name>
      <Point><coordinates>-73.9,40.7,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""

#: An external DTD subset with no entity declarations at all. Neither expat nor
#: lxml fetches one by default, so this is not exploitable today - it is here
#: because "no doctype" is a property that stays true regardless of what a parser
#: default does next year, and defusedxml's own defaults do NOT cover it
#: (forbid_dtd is False unless asked for).
EXTERNAL_DTD_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE kml SYSTEM "http://attacker.example.com/evil.dtd">
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>x</name>
      <Point><coordinates>-73.9,40.7,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""

BENIGN_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>An ordinary place</name>
      <Point><coordinates>-73.9,40.7,0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""


class KmlXxeTests(TestCase):
    """Entity-bearing KML is refused; ordinary KML still imports."""

    def setUp(self) -> None:
        """A profile to attribute imported pins to."""
        self.profile: Profile = baker.make(Profile)

    def _parse(self, payload: bytes) -> list[dict]:
        """Run the KML importer over *payload*."""
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

        return GoogleMapsGateway().takeout_kml_to_dict(payload, self.profile)

    def test_external_entity_is_refused(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import IMPORT_PARSE_ERRORS

        with self.assertRaises(IMPORT_PARSE_ERRORS):
            self._parse(XXE_KML)

    def test_entity_expansion_is_refused(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import IMPORT_PARSE_ERRORS

        with self.assertRaises(IMPORT_PARSE_ERRORS):
            self._parse(BILLION_LAUGHS_KML)

    def test_external_dtd_is_refused(self) -> None:
        from urbanlens.dashboard.services.apis.locations.google.maps import IMPORT_PARSE_ERRORS

        with self.assertRaises(IMPORT_PARSE_ERRORS):
            self._parse(EXTERNAL_DTD_KML)

    def test_an_ordinary_kml_still_imports(self) -> None:
        # The hardening must not cost the feature: a KML with no DTD parses as
        # before, including the https-namespace normalisation applied first.
        pins = self._parse(BENIGN_KML)
        self.assertEqual([pin["name"] for pin in pins], ["An ordinary place"])


class KmzXxeTests(SimpleTestCase):
    """The KMZ path unwraps to the same parser, so it inherits the same guard."""

    def test_the_guard_is_applied_before_fastkml(self) -> None:
        # Ordering matters more than it looks: fastkml/lxml resolving an entity
        # during its own parse is the thing being prevented, so a check that ran
        # afterwards would prevent nothing.
        import inspect

        from urbanlens.dashboard.services.apis.locations.google import maps

        source = inspect.getsource(maps.GoogleMapsGateway.takeout_kml_to_dict)
        self.assertLess(source.index("parse_xml_defused"), source.index("kml.KML.from_string"))
