"""Article > Sources lists every document its sources found, whatever a gallery's limit or a page's own record leaves out (P234)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from unittest.mock import patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider
from urbanlens.dashboard.services.pins.external_data import DocumentPanelSource, get_panel_source
from urbanlens.dashboard.services.pins.source_documents import documents_from_payloads


def _hrsh(**fields) -> Location:
    return Location(
        latitude=Decimal("41.733280"),
        longitude=Decimal("-73.928120"),
        official_name="Hudson River State Hospital",
        official_name_source="wikipedia",
        locality="Poughkeepsie",
        administrative_area_level_2="Dutchess County",
        administrative_area_level_1="NY",
        country="United States",
        **fields,
    )


def _image(index: int) -> MediaItem:
    return MediaItem(
        url=f"https://archive.test/photo-{index}.jpg", thumb_url="", caption=f"Photo {index}", source="probe"
    )


def _book(index: int, title: str = "Hudson River State Hospital annual report") -> MediaItem:
    return MediaItem(
        url=f"https://archive.test/book-{index}.pdf", thumb_url="", caption=title, source="probe", title=title
    )


class GalleryLimitTests(SimpleTestCase):
    """A provider's gallery limit counts tiles; the documents it finds all reach Sources."""

    def test_documents_ranked_after_a_full_gallery_are_kept(self) -> None:
        found = [*(_image(index) for index in range(30)), _book(1), _book(2)]

        @dataclass(slots=True, kw_only=True)
        class Provider(MediaProvider):
            service_key = "limit_probe"

            def _generate_media(self, search_term: str, address: str | None = None):
                yield from found

        with (
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.get_fresh", return_value=None),
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set"),
        ):
            items, _cached = Provider().get_media(_hrsh(), ["Hudson River State Hospital"], limit=24)

        self.assertEqual([item.url for item in items if item.is_document], [_book(1).url, _book(2).url])
        self.assertEqual(sum(1 for item in items if not item.is_document), 24)


def _cris_payload(resource_uuid: str, subject: str, attachment_ids: list[int]) -> dict:
    return {
        "resource_uuid": resource_uuid,
        "attachments_fetched": True,
        "site_scope": False,
        "attachments": [
            {
                "id": attachment_id,
                "kind": "document",
                "content_type": "application/pdf",
                "name": "Building Inventory Form",
                "resource_uuid": resource_uuid,
                "subject": subject,
                "subject_kind": "building",
            }
            for attachment_id in attachment_ids
        ],
    }


class NestedDocumentsTests(SimpleTestCase):
    """A nested building's cached documents are listed on its site's page, each once."""

    def test_each_childs_records_are_listed_once(self) -> None:
        cris = CrisBuildingPanelSource()
        payloads = [
            (_hrsh(pk=2), cris, _cris_payload("b-33", "BLDG 33/POWERHOUSE", [331, 332])),
            (_hrsh(pk=3), cris, _cris_payload("b-33", "BLDG 33/POWERHOUSE", [331])),
            (_hrsh(pk=4), cris, _cris_payload("b-35", "BLDG 35/PROTESTANT CHAPEL", [351])),
        ]

        listed = documents_from_payloads(payloads)

        self.assertEqual([item.document.document_id for item in listed], ["b-33.331", "b-33.332", "b-35.351"])
        self.assertEqual(listed[2].document.subject, "BLDG 35/PROTESTANT CHAPEL")

    def test_a_childs_search_results_are_judged_against_the_child(self) -> None:
        commons = get_panel_source("wikimedia")
        assert isinstance(commons, DocumentPanelSource)
        about = _book(1, "Hudson River State Hospital, Poughkeepsie: report of the superintendent")
        unrelated = _book(2, "Binghamton State Hospital, Binghamton, N.Y.: report of the superintendent")
        data = {"items": [asdict(about), asdict(unrelated)]}

        listed = documents_from_payloads([(_hrsh(), commons, data)])

        self.assertEqual([item.document.page_url for item in listed], [about.url])
