"""P228: a known National Register listing links to NPS's record, and that link joins the pin's and wiki's links."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.links.model import AutoLinkSource, PinLink, WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.plugins.builtin.redata_historic_registers import HistoricRegisterPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.locations.external_links import add_pin_and_wiki_link, add_pin_link, add_wiki_link
from urbanlens.dashboard.services.locations.national_register import nps_record_url, reference_number

#: Hudson River State Hospital, Main Building: NPS's own PROPERTY_ID, as REData's nps_nrhp row carries it.
_REFERENCE = "89001166"
#: Checked live 2026-10-03: this page renders the listing; a made-up number renders an empty page, also as a 200.
_NPS_URL = "https://npgallery.nps.gov/AssetDetail/NRIS/89001166"
_LISTING_NAME = "Hudson River State Hospital, Main Building"
_AUTO_TITLE = "Added automatically from National Register of Historic Places"


def _listing(**overrides) -> dict:
    return {
        "provider": "nps_nrhp",
        "resource_type": "building",
        "scope": "structure",
        "external_id": _REFERENCE,
        "name": _LISTING_NAME,
        "status": "Listed",
        "year_built": None,
        "architectural_style": "",
        "use_type": "",
        "contains_point": True,
        "source_latitude": None,
        "source_longitude": None,
        **overrides,
    }


class ReferenceNumberTests(SimpleTestCase):
    """Only a number NPS's own layer published is linked, and only as NPS's record."""

    def test_an_nrhp_reference_number_links_to_npss_record(self) -> None:
        self.assertEqual(nps_record_url(_REFERENCE), _NPS_URL)

    def test_a_modern_nine_digit_reference_number_is_one(self) -> None:
        self.assertEqual(nps_record_url("100001066"), "https://npgallery.nps.gov/AssetDetail/NRIS/100001066")

    def test_nyshpos_own_number_is_not_npss(self) -> None:
        """CRIS's NRNum (``94NR00622``) is SHPO's file number; NPGallery shows an empty page for it."""
        for value in ("94NR00622", "", "8900116", "1234567890", "89001166/../x", None):
            with self.subTest(value=value):
                self.assertIsNone(nps_record_url(value))

    def test_only_ascii_digits_make_a_reference_number(self) -> None:
        """``\\d`` also matches full-width and other scripts' digits, which are not NPS's number."""
        for value in ("８９００１１６６", "٨٩٠٠١١٦٦", "89001¹66"):
            with self.subTest(value=value):
                self.assertIsNone(nps_record_url(value))
                self.assertIsNone(reference_number(_listing(external_id=value)))

    def test_only_the_national_registers_rows_carry_one(self) -> None:
        self.assertEqual(reference_number(_listing()), _REFERENCE)
        self.assertIsNone(reference_number(_listing(provider="md_mihp")))
        self.assertIsNone(reference_number(_listing(external_id="")))
        self.assertIsNone(reference_number({"provider": "nps_nrhp"}))

    def test_the_cached_row_keeps_the_reference_number(self) -> None:
        stored = HistoricRegisterPanelSource().transform_rows(
            [
                {
                    **_listing(source_latitude=41.7, source_longitude=-73.9),
                    "attributes": {"NARA_URL": "x"},
                    "geometry": {},
                }
            ],
        )

        self.assertEqual(stored[0]["external_id"], _REFERENCE)
        self.assertEqual((stored[0]["source_latitude"], stored[0]["source_longitude"]), (41.7, -73.9))
        self.assertNotIn("attributes", stored[0])


class _PinBase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.location = baker.make(Location, latitude="41.733100", longitude="-73.928600")
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location, parent_pin=None)
        self.source = HistoricRegisterPanelSource()


class HistoricRegisterPanelLinkTests(_PinBase):
    """Property Records > Historic Registers names the reference number and links it."""

    def test_the_listing_row_links_its_reference_number(self) -> None:
        context = self.source.render_context(self.pin, {"resources": [_listing()]})

        assert context is not None
        entry = context["meta"][0]
        self.assertEqual(entry["href"], _NPS_URL)
        self.assertIn(_REFERENCE, entry["value"])

    def test_the_listing_note_links_the_record_too(self) -> None:
        context = self.source.render_context(self.pin, {"resources": [_listing()]})

        assert context is not None
        self.assertEqual(context["facts"][0]["href"], _NPS_URL)

    def test_another_registers_row_is_not_linked(self) -> None:
        context = self.source.render_context(
            self.pin, {"resources": [_listing(provider="md_mihp", external_id="D-1", name="Hutzler")]}
        )

        assert context is not None
        self.assertNotIn("href", context["meta"][0])


class CrisCardReferenceTests(_PinBase):
    """The CRIS card's National Register number is NYSHPO's; NPS's reference number is shown beside it, linked."""

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
            },
        }

    def test_nyshpos_number_is_labelled_as_nyshpos(self) -> None:
        context = self.cris.render_context(self.site_pin, self.data)

        assert context is not None
        self.assertIn({"label": "NYSHPO National Register Number", "value": "94NR00622"}, context["meta"])
        self.assertNotIn("National Register Number", [entry["label"] for entry in context["meta"]])

    def test_npss_reference_number_for_the_same_listing_is_linked(self) -> None:
        LocationCache.set(self.location, "redata_historic_registers", {"resources": [_listing()]})

        context = self.cris.render_context(self.site_pin, self.data)

        assert context is not None
        self.assertIn({"label": "NRHP Reference Number", "value": _REFERENCE, "href": _NPS_URL}, context["meta"])

    def test_a_differently_named_listing_is_not_claimed(self) -> None:
        LocationCache.set(
            self.location, "redata_historic_registers", {"resources": [_listing(name="Roosevelt, Isaac, House")]}
        )

        context = self.cris.render_context(self.site_pin, self.data)

        assert context is not None
        self.assertNotIn("NRHP Reference Number", [entry["label"] for entry in context["meta"]])


class AutomaticLinkTests(_PinBase):
    """Fetching the registers adds NPS's record to the pin's and the wiki's links, once, marked automatic."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")

    def _fetch(self, *rows: dict) -> None:
        envelope = LocationContextEnvelope(count=len(rows), complete=True, results=[dict(row) for row in rows])
        with mock.patch.object(HistoricRegisterPanelSource, "fetch_envelope", return_value=envelope):
            self.source.fetch(self.pin)

    def test_the_listing_joins_the_pins_and_the_wikis_links(self) -> None:
        self._fetch(_listing())

        pin_link = PinLink.objects.get(pin=self.pin, url=_NPS_URL)
        wiki_link = WikiLink.objects.get(wiki=self.wiki, url=_NPS_URL)
        self.assertEqual(pin_link.auto_source, AutoLinkSource.NATIONAL_REGISTER)
        self.assertEqual(wiki_link.auto_source, AutoLinkSource.NATIONAL_REGISTER)
        self.assertIn(_REFERENCE, pin_link.name)

    def test_a_second_fetch_adds_nothing(self) -> None:
        self._fetch(_listing())
        self._fetch(_listing())

        self.assertEqual(PinLink.objects.filter(pin=self.pin, url=_NPS_URL).count(), 1)
        self.assertEqual(WikiLink.objects.filter(wiki=self.wiki, url=_NPS_URL).count(), 1)

    def test_a_link_the_owner_removed_stays_removed(self) -> None:
        self._fetch(_listing())
        self.client.force_login(self.user)
        link = PinLink.objects.get(pin=self.pin, url=_NPS_URL)
        self.client.delete(reverse("pin.link.delete", args=[self.pin.slug, link.pk]))

        self._fetch(_listing())

        self.assertFalse(PinLink.objects.filter(pin=self.pin, url=_NPS_URL).exists())
        self.assertTrue(PinAutoRemoval.objects.was_removed(pin=self.pin, kind=AutoRemovalKind.LINK, value=_NPS_URL))

    def test_a_neighbours_listing_is_not_this_places_link(self) -> None:
        """REData's 250 m search also finds listings whose boundary does not hold the pin."""
        self._fetch(_listing(contains_point=False, name="Roosevelt, Isaac, House", external_id="72000839"))

        self.assertFalse(PinLink.objects.filter(pin=self.pin).exists())
        self.assertFalse(WikiLink.objects.filter(wiki=self.wiki).exists())

    def test_the_same_link_added_by_hand_stays_the_owners(self) -> None:
        PinLink.objects.create(pin=self.pin, url=_NPS_URL, name="NHL nomination")

        self._fetch(_listing())

        link = PinLink.objects.get(pin=self.pin, url=_NPS_URL)
        self.assertEqual((link.name, link.auto_source), ("NHL nomination", ""))


class AttributionTests(_PinBase):
    """Every automatic adder names its source; a link a person adds names none."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=self.location, name="Mill")

    def test_the_helpers_record_the_source(self) -> None:
        add_pin_link(self.pin, "https://example.test/a", "A", source=AutoLinkSource.WIKIPEDIA)
        add_wiki_link(self.wiki, "https://example.test/b", "B", source=AutoLinkSource.WIKIPEDIA)
        add_pin_and_wiki_link(self.pin, self.location, "https://example.test/c", "C", source=AutoLinkSource.EPA_ECHO)

        self.assertEqual(PinLink.objects.get(url="https://example.test/a").auto_source, AutoLinkSource.WIKIPEDIA)
        self.assertEqual(WikiLink.objects.get(url="https://example.test/b").auto_source, AutoLinkSource.WIKIPEDIA)
        self.assertEqual(PinLink.objects.get(url="https://example.test/c").auto_source, AutoLinkSource.EPA_ECHO)
        self.assertEqual(WikiLink.objects.get(url="https://example.test/c").auto_source, AutoLinkSource.EPA_ECHO)

    def test_openstreetmap_links_say_so(self) -> None:
        from urbanlens.dashboard.plugins.builtin.nominatim import NominatimPanelSource

        NominatimPanelSource._add_osm_link(self.pin, self.location, "https://www.openstreetmap.org/way/123")

        self.assertEqual(PinLink.objects.get(pin=self.pin).auto_source, AutoLinkSource.OPENSTREETMAP)
        self.assertEqual(WikiLink.objects.get(wiki=self.wiki).auto_source, AutoLinkSource.OPENSTREETMAP)

    def test_epa_links_say_so(self) -> None:
        from urbanlens.dashboard.plugins.builtin.epa_echo import EpaEchoDetailPanelSource

        EpaEchoDetailPanelSource._add_echo_report_link(self.pin, self.location, "110000123")

        self.assertEqual(PinLink.objects.get(pin=self.pin).auto_source, AutoLinkSource.EPA_ECHO)

    def test_wikipedia_links_say_so(self) -> None:
        from urbanlens.dashboard.services.wiki.wiki_seed import seed_pin_from_cached_wikipedia

        LocationCache.set(
            self.location,
            "wikipedia",
            {"title": "Mill", "url": "https://en.wikipedia.org/wiki/Mill", "extract": "A mill."},
        )
        with mock.patch("urbanlens.dashboard.services.wiki.wiki_seed.seed_pin_article_from_wikipedia"):
            seed_pin_from_cached_wikipedia(self.pin)

        self.assertEqual(PinLink.objects.get(pin=self.pin).auto_source, AutoLinkSource.WIKIPEDIA)

    def test_a_link_a_person_adds_names_no_source(self) -> None:
        self.client.force_login(self.user)
        self.client.post(
            reverse("pin.links", args=[self.pin.slug]), {"url": "https://example.test/mine", "name": "Mine"}
        )

        self.assertEqual(PinLink.objects.get(pin=self.pin).auto_source, "")


class CacheMigrationTests(_PinBase):
    """Rows cached before the reference number was kept are dropped, so they refetch with it."""

    def test_the_migration_drops_only_the_registers_rows(self) -> None:
        import importlib

        from django.apps import apps

        migration = importlib.import_module(
            "urbanlens.dashboard.migrations.0046_location_cache_drop_historic_registers"
        )
        LocationCache.set(self.location, "redata_historic_registers", {"resources": [{"name": _LISTING_NAME}]})
        LocationCache.set(self.location, "cris_building_usn", {"USNName": "BLDG 51"})

        migration.drop_historic_registers(apps, None)

        self.assertEqual(
            list(LocationCache.objects.filter(location=self.location).values_list("source", flat=True)),
            ["cris_building_usn"],
        )


class LinkChipTests(_PinBase):
    """A link added automatically says so, and from where."""

    def setUp(self) -> None:
        super().setUp()
        self.wiki = baker.make(Wiki, location=self.location, name="Hudson River State Hospital")
        self.client.force_login(self.user)

    def test_the_pins_automatic_link_names_its_source(self) -> None:
        add_pin_link(self.pin, _NPS_URL, "National Register", source=AutoLinkSource.NATIONAL_REGISTER)
        PinLink.objects.create(pin=self.pin, url="https://example.test/mine", name="Mine")

        content = self.client.get(reverse("pin.links", args=[self.pin.slug])).content.decode()

        self.assertEqual(content.count(f'title="{_AUTO_TITLE}"'), 1)
        self.assertEqual(content.count('title="Added automatically'), 1, "the owner's own link is not marked")

    def test_the_wikis_automatic_link_names_its_source(self) -> None:
        add_wiki_link(self.wiki, _NPS_URL, "National Register", source=AutoLinkSource.NATIONAL_REGISTER)

        response = self.client.get(reverse("location.wiki.links", args=[self.location.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _AUTO_TITLE)
