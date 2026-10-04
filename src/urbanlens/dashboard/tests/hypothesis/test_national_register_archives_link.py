"""P256: a National Register listing whose REData row carries ``attributes.NARA_URL`` links the National Archives record.

NPGallery renders an empty page, as a 200, for listings it does not carry (most after 2012), so the archives' own
record of the listing is linked beside it wherever REData published one. The URL comes from a third party, so only a
National Archives catalogue record is ever rendered.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from model_bakery import baker

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.links.model import AutoLinkSource, PinLink, WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.plugins.builtin.redata_historic_registers import HistoricRegisterPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.locations.national_register import NARA_RECORD_LABEL, nara_record_url

_REFERENCE = "100007768"
_NPS_URL = "https://npgallery.nps.gov/AssetDetail/NRIS/100007768"
#: The shape REData's nps_nrhp rows carry (``nrhp.gateway``'s captured live feature).
_NARA_URL = "https://catalog.archives.gov/id/71997138"
_LISTING_NAME = "Hudson River State Hospital, Main Building"

_HOSTILE = (
    "javascript:alert(1)",
    "JaVaScRiPt:alert(document.cookie)",
    "data:text/html,<script>alert(1)</script>",
    'https://catalog.archives.gov/id/1"><script>alert(1)</script>',
    "https://catalog.archives.gov.evil.example/id/71997138",
    "https://evil.example/?next=https://catalog.archives.gov/id/71997138",
    "https://evil.example@catalog.archives.gov/id/71997138",
    "https://catalog.archives.gov@evil.example/id/71997138",
    "https://catalog.archives.gov:8443/id/71997138",
    "https://catalog.archives.gov/search?q=hudson",
    "https://catalog.archives.gov/id/71997138/../../redirect?u=evil",
    "//catalog.archives.gov/id/71997138",
    "ftp://catalog.archives.gov/id/71997138",
    "https://catalog.archives.gov/id/",
    "https://catalog.archives.gov/id/７１９９",
    " https://catalog.archives.gov/id/71997138",
    "",
)


def _listing(**overrides) -> dict:
    return {
        "provider": "nps_nrhp",
        "resource_type": "building",
        "scope": "structure",
        "external_id": _REFERENCE,
        "name": _LISTING_NAME,
        "status": "Listed",
        "contains_point": True,
        "source_latitude": None,
        "source_longitude": None,
        "nara_url": _NARA_URL,
        **overrides,
    }


class NaraRecordUrlTests(SimpleTestCase):
    def test_a_catalogue_record_is_linked(self) -> None:
        self.assertEqual(nara_record_url(_NARA_URL), _NARA_URL)

    def test_the_insecure_form_of_a_catalogue_record_is_linked_over_https(self) -> None:
        self.assertEqual(nara_record_url("http://catalog.archives.gov/id/71997138"), _NARA_URL)
        self.assertEqual(nara_record_url("HTTPS://Catalog.Archives.gov/id/71997138/"), _NARA_URL)

    def test_anything_else_is_not(self) -> None:
        for value in (*_HOSTILE, None, 71997138, ["https://catalog.archives.gov/id/1"]):
            with self.subTest(value=value):
                self.assertIsNone(nara_record_url(value))

    @given(st.text())
    def test_whatever_arrives_only_a_catalogue_record_comes_out(self, value: str) -> None:
        url = nara_record_url(value)
        if url is not None:
            self.assertRegex(url, r"\Ahttps://catalog\.archives\.gov/id/[0-9]+\Z")


class CachedRowTests(SimpleTestCase):
    def _stored(self, row: dict) -> dict:
        return HistoricRegisterPanelSource().transform_rows([row])[0]

    def test_the_cached_row_keeps_the_archives_record(self) -> None:
        stored = self._stored({**_listing(nara_url=None), "attributes": {"NARA_URL": _NARA_URL}, "geometry": {}})

        self.assertEqual(stored["nara_url"], _NARA_URL)
        self.assertNotIn("attributes", stored)

    def test_a_url_that_is_not_a_catalogue_record_is_not_cached(self) -> None:
        stored = self._stored({**_listing(nara_url=None), "attributes": {"NARA_URL": "javascript:alert(1)"}})

        self.assertNotIn("nara_url", stored)

    def test_another_registers_attributes_are_not_read(self) -> None:
        stored = self._stored({**_listing(provider="md_mihp", nara_url=None), "attributes": {"NARA_URL": _NARA_URL}})

        self.assertNotIn("nara_url", stored)


class _PinBase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.location = baker.make(Location, latitude="41.733100", longitude="-73.928600")
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, parent_pin=None)
        self.source = HistoricRegisterPanelSource()

    def _context(self, *rows: dict) -> dict:
        context = self.source.render_context(self.pin, {"resources": list(rows)})
        assert context is not None
        return context


class HistoricRegistersCardTests(_PinBase):
    def test_the_listing_links_the_archives_record_beside_npgallery(self) -> None:
        meta = self._context(_listing())["meta"]

        hrefs = [entry.get("href") for entry in meta]
        self.assertEqual(hrefs[:2], [_NPS_URL, _NARA_URL])
        self.assertEqual(meta[1]["label"], NARA_RECORD_LABEL)

    def test_the_note_names_the_archives_record_as_well(self) -> None:
        facts = self._context(_listing())["facts"]

        self.assertEqual([fact.get("href") for fact in facts], [_NPS_URL, _NARA_URL])
        self.assertIn("National Archives", facts[1]["text"])

    def test_a_row_cached_before_the_archives_record_was_kept_shows_npgallery_alone(self) -> None:
        row = _listing()
        del row["nara_url"]

        context = self._context(row)

        self.assertEqual([entry.get("href") for entry in context["meta"]], [_NPS_URL])
        self.assertEqual([fact.get("href") for fact in context["facts"]], [_NPS_URL])

    def test_a_tampered_cache_row_renders_no_link(self) -> None:
        """A cached value is checked again where it is rendered, not trusted for having been checked once."""
        for value in _HOSTILE:
            with self.subTest(value=value):
                context = self._context(_listing(nara_url=value))

                html = render_to_string("dashboard/partials/pins/_simple_info_body.html", context)
                self.assertNotIn("javascript", html.lower())
                self.assertNotIn("evil.example", html)
                self.assertEqual([entry.get("href") for entry in context["meta"]], [_NPS_URL])

    def test_the_overview_gives_the_archives_record(self) -> None:
        summary = self.source.overview_summary(self.pin, {"resources": [_listing()]})

        assert summary is not None
        self.assertIn({"label": NARA_RECORD_LABEL, "value": "Catalog ID 71997138", "href": _NARA_URL}, summary.fields)
        self.assertEqual([field.get("href") for field in summary.fields][:2], [_NPS_URL, _NARA_URL])


class CrisCardTests(_PinBase):
    def setUp(self) -> None:
        super().setUp()
        self.cris = CrisBuildingPanelSource()
        self.site_pin = SimpleNamespace(_site_scope_cache=True, location=self.location)
        self.data = {
            "USNName": "BLDG 51/MAIN/ADMIN (1871) - NHL",
            "district": {
                "HistoricName": _LISTING_NAME,
                "NRNum": "94NR00622",
                "resource_type": "national_register_listing",
                "contains_point": True,
                "EligibilityDesc": "Listed",
            },
        }
        LocationCache.set(self.location, "redata_historic_registers", {"resources": [_listing()]})

    def test_the_cris_card_links_the_archives_record_beside_npss(self) -> None:
        context = self.cris.render_context(self.site_pin, self.data)

        assert context is not None
        self.assertIn({"label": NARA_RECORD_LABEL, "value": "Catalog ID 71997138", "href": _NARA_URL}, context["meta"])

    def test_the_cris_overview_gives_it_too(self) -> None:
        summary = self.cris.overview_summary(self.site_pin, self.data)

        assert summary is not None
        self.assertIn({"label": NARA_RECORD_LABEL, "value": "Catalog ID 71997138", "href": _NARA_URL}, summary.fields)


class AutomaticLinkTests(_PinBase):
    def test_fetching_adds_the_archives_record_to_the_pins_and_wikis_links(self) -> None:
        wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")
        row = {**_listing(nara_url=None), "attributes": {"NARA_URL": _NARA_URL}}
        envelope = LocationContextEnvelope(count=1, complete=True, results=[row])

        with mock.patch.object(HistoricRegisterPanelSource, "fetch_envelope", return_value=envelope):
            self.source.fetch(self.pin)

        self.assertTrue(PinLink.objects.filter(pin=self.pin, url=_NPS_URL).exists())
        archives = PinLink.objects.get(pin=self.pin, url=_NARA_URL)
        self.assertEqual(archives.auto_source, AutoLinkSource.NATIONAL_REGISTER)
        self.assertIn("National Archives", archives.name)
        self.assertTrue(WikiLink.objects.filter(wiki=wiki, url=_NARA_URL).exists())
