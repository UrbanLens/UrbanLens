"""The Private Pin page's Photos tab lists every photo the pin has: the user's own, filed in an album or not,
and the public-source photos the Media panel shows."""

from __future__ import annotations

import re
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import get_template
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.services.photos.pin_photos import MAX_PAGE_SIZE, PIN_MEDIA_GALLERY_SOURCES

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
    b"\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)

_SCHEDULE = "urbanlens.dashboard.services.photos.pin_photos.schedule_panel_fetch"


def _wikimedia_item(n: int, **overrides) -> dict:
    item = {
        "url": f"https://upload.wikimedia.org/hrsh/{n}.jpg",
        "thumb_url": f"https://upload.wikimedia.org/hrsh/thumb/{n}.jpg",
        "caption": f"Kirkbride building {n}",
        "source": "Wikimedia Commons",
        "page_url": f"https://commons.wikimedia.org/wiki/File:HRSH_{n}.jpg",
        "content_type": "",
        "author": "Daniel Case",
    }
    item.update(overrides)
    return item


class PinPhotosTabTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.pin = baker.make_recipe("dashboard.pin", name="Hudson River State Hospital")
        self.profile = self.pin.profile
        self.client.force_login(self.profile.user)
        patcher = patch(_SCHEDULE, return_value=False)
        self.schedule = patcher.start()
        self.addCleanup(patcher.stop)

    def _photo(self, name: str = "photo.png", **fields) -> Image:
        fields.setdefault("pin", self.pin)
        fields.setdefault("profile", self.profile)
        return baker.make(Image, image=SimpleUploadedFile(name, _PNG_BYTES, content_type="image/png"), **fields)

    def _cache(self, source: str, items: list[dict]) -> None:
        from urbanlens.dashboard.services.pins.external_data import get_panel_source

        panel = get_panel_source(source)
        assert panel is not None
        LocationCache.set(
            self.pin.location, panel.cache_source, {"items": items}, query_key="hudson river state hospital"
        )

    def _get(self, **params):
        url = reverse("pin.albums", args=[self.pin.slug])
        return self.client.get(f"{url}?{urlencode(params)}" if params else url)

    def _json(self, **params) -> dict:
        response = self._get(**params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def _all(self, kind: str) -> list[dict]:
        items: list[dict] = []
        while True:
            body = self._json(**{kind: 1, "offset": len(items), "limit": MAX_PAGE_SIZE})
            items.extend(body["items"])
            if not body["items"] or len(items) >= body["total"]:
                return items


class OwnPhotosTests(PinPhotosTabTestCase):
    def test_own_photos_are_listed_whether_or_not_they_are_in_an_album(self) -> None:
        loose = self._photo("loose.png")
        filed = self._photo("filed.png")
        album = Album.objects.create(name="Kirkbride", profile=self.profile, parent_pin=self.pin)
        AlbumItem.objects.create(album=album, image=filed, added_by=self.profile)

        by_id = {item["id"]: item for item in self._all("mine")}

        self.assertEqual(set(by_id), {loose.pk, filed.pk})
        self.assertFalse(by_id[loose.pk]["in_album"])
        self.assertTrue(by_id[filed.pk]["in_album"])
        for item in by_id.values():
            self.assertEqual(item["origin"], "own")
            self.assertTrue(item["is_mine"])

    def test_a_photo_still_processing_names_no_file(self) -> None:
        pending = self._photo("pending.png", pending_scan=True)

        [item] = self._all("mine")

        self.assertEqual(item["id"], pending.pk)
        self.assertTrue(item["processing"])
        self.assertEqual(item["url"], "")
        self.assertEqual(item["thumb_url"], "")

    def test_another_accounts_photos_at_the_same_place_are_not_listed(self) -> None:
        other = baker.make_recipe("dashboard.pin", location=self.pin.location)
        self._photo("theirs.png", pin=other, profile=other.profile)
        mine = self._photo("mine.png")

        self.assertEqual([item["id"] for item in self._all("mine")], [mine.pk])

    def test_a_materialized_public_photo_says_which_media_item_it_is(self) -> None:
        url = "https://upload.wikimedia.org/hrsh/1.jpg"
        self._photo("copy.png", media_source_key="wikimedia", media_item_key=media_item_key(url))

        [item] = self._all("mine")

        self.assertEqual(item["media_key"], media_item_key(url))


class ExternalPhotosTests(PinPhotosTabTestCase):
    def test_public_source_photos_are_listed_with_attribution(self) -> None:
        self._cache("wikimedia", [_wikimedia_item(1)])

        [item] = self._all("external")

        self.assertEqual(item["origin"], "external")
        self.assertEqual(item["source"], "wikimedia")
        self.assertEqual(item["source_name"], "Wikimedia Commons")
        self.assertEqual(item["author"], "Daniel Case")
        self.assertEqual(item["page_url"], "https://commons.wikimedia.org/wiki/File:HRSH_1.jpg")
        self.assertEqual(item["url"], "https://upload.wikimedia.org/hrsh/1.jpg")
        self.assertEqual(item["thumb_url"], "https://upload.wikimedia.org/hrsh/thumb/1.jpg")
        self.assertEqual(item["key"], media_item_key("https://upload.wikimedia.org/hrsh/1.jpg"))
        self.assertFalse(item["is_mine"])

    def test_photos_from_several_sources_are_all_listed(self) -> None:
        for n, source in enumerate(("wikimedia", "loc", "smithsonian")):
            self._cache(source, [_wikimedia_item(n, source=source)])

        listed = {item["source"] for item in self._all("external")}

        self.assertEqual(listed, {"wikimedia", "loc", "smithsonian"})

    def test_a_photo_both_sources_return_is_listed_once(self) -> None:
        self._cache("wikimedia", [_wikimedia_item(1)])
        self._cache("wikipedia_media", [_wikimedia_item(1, source="Wikipedia")])

        self.assertEqual(len(self._all("external")), 1)

    def test_a_photo_already_saved_to_this_pin_is_listed_once_as_the_users_own(self) -> None:
        item = _wikimedia_item(1)
        self._cache("wikimedia", [item, _wikimedia_item(2)])
        self._photo("copy.png", media_source_key="wikimedia", media_item_key=media_item_key(item["url"]))

        external = self._all("external")

        self.assertEqual([entry["url"] for entry in external], [_wikimedia_item(2)["url"]])

    def test_items_marked_not_relevant_and_items_with_no_image_are_left_out(self) -> None:
        rejected = _wikimedia_item(1)
        text_only = _wikimedia_item(2, url="https://example.org/record/2", thumb_url="")
        kept = _wikimedia_item(3)
        self._cache("wikimedia", [rejected, text_only, kept])
        MediaRelevance.objects.create(
            profile=self.profile,
            location=self.pin.location,
            source="wikimedia",
            item_key=media_item_key(rejected["url"]),
            is_relevant=False,
        )

        self.assertEqual([entry["url"] for entry in self._all("external")], [kept["url"]])

    def test_an_unrenderable_original_is_shown_through_the_preview_route(self) -> None:
        self._cache("wikimedia", [_wikimedia_item(1, url="https://upload.wikimedia.org/hrsh/scan.tif", thumb_url="")])

        [item] = self._all("external")

        self.assertNotIn(".tif", item["thumb_url"].split("?")[0])
        self.assertTrue(item["thumb_url"].startswith(reverse("media.preview")))

    def test_pages_are_bounded(self) -> None:
        self._cache("wikimedia", [_wikimedia_item(n) for n in range(MAX_PAGE_SIZE + 30)])

        body = self._json(external=1, offset=0, limit=10_000)

        self.assertEqual(body["total"], MAX_PAGE_SIZE + 30)
        self.assertEqual(len(body["items"]), MAX_PAGE_SIZE)

    def test_a_source_still_fetching_is_reported_as_pending(self) -> None:
        self.schedule.return_value = True

        body = self._json(external=1)

        self.assertIn("wikimedia", body["pending"])
        self.assertEqual(body["items"], [])

    def test_a_source_that_answered_is_not_pending(self) -> None:
        self.schedule.return_value = True
        self._cache("wikimedia", [])

        self.assertNotIn("wikimedia", self._json(external=1)["pending"])

    def test_a_stale_answer_is_refetched_rather_than_listed(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        self.schedule.return_value = True
        self._cache("wikimedia", [_wikimedia_item(1)])
        LocationCache.objects.filter(location=self.pin.location, source="wikimedia").update(
            updated=timezone.now() - timedelta(days=3650)
        )

        body = self._json(external=1)

        self.assertEqual(body["items"], [])
        self.assertIn("wikimedia", body["pending"])

    def test_the_source_list_matches_the_media_panels_loaders(self) -> None:
        source = get_template("dashboard/pages/location/index.html").template.source
        loaders = set(re.findall(r'id="media-loader-([a-z_]+)"', source)) - {"photos"}

        self.assertEqual(loaders, set(PIN_MEDIA_GALLERY_SOURCES))


class PhotosPanelTests(PinPhotosTabTestCase):
    def test_the_panel_counts_album_photos_among_the_users_own(self) -> None:
        self._photo("loose.png")
        filed = self._photo("filed.png")
        album = Album.objects.create(name="Kirkbride", profile=self.profile, parent_pin=self.pin)
        AlbumItem.objects.create(album=album, image=filed, added_by=self.profile)

        response = self._get()

        self.assertContains(response, 'data-photo-count="2"')
        self.assertContains(response, "Not in an album")

    def test_the_panel_loads_the_public_source_section(self) -> None:
        response = self._get()

        self.assertContains(response, "external_section=1")

    def test_item_urls_keep_the_children_flag_well_formed(self) -> None:
        self._photo("loose.png")

        body = self._get(children=1).content.decode()

        self.assertNotRegex(body, r"\?children=1\?")
        self.assertIn("children=1&amp;mine=1", body)

    def test_the_public_source_section_shows_its_count_and_grid(self) -> None:
        self._cache("wikimedia", [_wikimedia_item(1), _wikimedia_item(2)])

        response = self._get(external_section=1)

        self.assertContains(response, "From public sources")
        self.assertContains(response, 'data-photo-count="2"')
        self.assertContains(response, "external=1")

    def test_the_public_source_section_polls_while_a_source_is_fetching(self) -> None:
        self.schedule.return_value = True

        response = self._get(external_section=1)

        self.assertContains(response, "attempt=1")

    def test_the_vault_has_no_public_source_listing(self) -> None:
        response = self.client.get(f"{reverse('vault.photos.albums')}?external=1")

        self.assertEqual(response.status_code, 404)
