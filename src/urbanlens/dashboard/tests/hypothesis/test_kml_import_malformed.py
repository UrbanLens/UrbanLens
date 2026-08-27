"""One malformed KML must skip that file, not abort the whole import.

The bulk importer's per-file handler exists precisely so a bad file is logged and
skipped while the rest of the user's upload proceeds. Its exception tuple listed
`XMLParseError` - which is `defusedxml.ElementTree.ParseError` - and neither of
the two errors a malformed KML actually produces is that type:

    fastkml KMLParseError  -> FastKMLError -> Exception
    lxml    XMLSyntaxError -> ParseError -> LxmlSyntaxError -> SyntaxError

Both escaped the parser's own handler *and* the caller's, so a KML with
unparseable coordinates or a truncated tag took down the entire import stream -
losing every other file in the same upload, not just the broken one.

Both failures are reachable from ordinary bad input, which is what these fixtures
are: coordinates that are not numbers, and a file cut off mid-tag.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile

_VALID_KML = b"""<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
<name>Somewhere</name><Point><coordinates>-73.7562,42.6526,0</coordinates></Point>
</Placemark></Document></kml>"""

#: Well-formed XML, valid KML structure, but the coordinates cannot be parsed.
_BAD_COORDINATES_KML = b"""<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
<name>Broken</name><Point><coordinates>not,a,coord</coordinates></Point>
</Placemark></Document></kml>"""

#: Cut off mid-tag - lxml raises before fastkml sees it.
_TRUNCATED_KML = b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>'


class MalformedKmlImportTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))
        from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway

        self.service = GoogleMapsGateway()

    def test_a_valid_kml_still_parses(self) -> None:
        """Anchors the others: the fixture format is right, so failures are real."""
        pins = self.service.takeout_kml_to_dict(_VALID_KML, self.profile)

        self.assertEqual(len(pins), 1)
        self.assertAlmostEqual(pins[0]["latitude"], 42.6526, places=3)

    def test_unparseable_coordinates_are_skippable(self) -> None:
        """fastkml's KMLParseError, asserted against the *production* tuple."""
        from urbanlens.dashboard.services.apis.locations.google.maps import IMPORT_PARSE_ERRORS

        with self.assertRaises(IMPORT_PARSE_ERRORS):
            self.service.takeout_kml_to_dict(_BAD_COORDINATES_KML, self.profile)

    def test_a_truncated_kml_is_skippable(self) -> None:
        """lxml's XMLSyntaxError, which is a SyntaxError - not a ValueError."""
        from urbanlens.dashboard.services.apis.locations.google.maps import IMPORT_PARSE_ERRORS

        with self.assertRaises(IMPORT_PARSE_ERRORS):
            self.service.takeout_kml_to_dict(_TRUNCATED_KML, self.profile)

    def test_the_importer_uses_that_one_tuple_in_both_handlers(self) -> None:
        """Two handlers needed the same list and drifted apart once already."""
        import inspect

        from urbanlens.dashboard.services.apis.locations.google import maps

        uses = inspect.getsource(maps).count("except IMPORT_PARSE_ERRORS")

        self.assertGreaterEqual(uses, 2, "both the KML parser and the per-file import guard should use it")
