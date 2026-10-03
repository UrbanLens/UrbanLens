"""A URL's tracking parameters never change what it identifies (P215)."""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.auth.models import User
from model_bakery import baker

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.core.tracking_params import without_tracking_params

_FILE = "https://upload.wikimedia.org/wikipedia/commons/6/68/Cheney_bldg.jpg"
_TRACKED = f"{_FILE}?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original"
_migration = importlib.import_module("urbanlens.dashboard.migrations.0043_media_keys_without_tracking_params")


def _sha1(url: str) -> str:
    import hashlib

    return hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()


class WithoutTrackingParamsTests(SimpleTestCase):
    def test_commons_utm_parameters_are_dropped(self) -> None:
        self.assertEqual(without_tracking_params(_TRACKED), _FILE)

    def test_other_parameters_and_their_order_are_kept(self) -> None:
        self.assertEqual(
            without_tracking_params("https://example.test/a.jpg?b=2&UTM_Medium=x&a=1#frag"),
            "https://example.test/a.jpg?b=2&a=1#frag",
        )

    def test_a_url_without_them_is_returned_unchanged(self) -> None:
        for url in (_FILE, "https://example.test/a?utmost=1", "https://example.test/a?x=%20y&x=2", "not a url", ""):
            with self.subTest(url=url):
                self.assertEqual(without_tracking_params(url), url)

    @given(st.text(alphabet="abc=&?%#/:.", max_size=40))
    def test_it_is_idempotent(self, url: str) -> None:
        once = without_tracking_params(url)
        self.assertEqual(without_tracking_params(once), once)


class MediaItemKeyTests(SimpleTestCase):
    def test_a_file_keeps_its_key_whether_or_not_commons_tracks_it(self) -> None:
        self.assertEqual(media_item_key(_TRACKED), media_item_key(_FILE))


class RekeyMigrationTests(TestCase):
    """Marks and copies keyed while a cached item's URL carried the parameters move to the key the item has now."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location)
        self.profile = baker.make(User).profile
        LocationCache.set(self.location, "wikimedia", {"items": [{"url": _TRACKED, "thumb_url": _TRACKED}]})

    def _run(self) -> None:
        _migration.rekey_tracked_media(apps, None)

    def test_a_relevance_mark_moves_to_the_clean_key(self) -> None:
        mark = baker.make(
            MediaRelevance,
            profile=self.profile,
            location=self.location,
            source="wikimedia",
            item_key=_sha1(_TRACKED),
            is_relevant=True,
        )

        self._run()

        mark.refresh_from_db()
        self.assertEqual(mark.item_key, media_item_key(_FILE))

    def test_a_mark_already_on_the_clean_key_wins_over_the_tracked_duplicate(self) -> None:
        baker.make(
            MediaRelevance,
            profile=self.profile,
            location=self.location,
            source="wikimedia",
            item_key=_sha1(_TRACKED),
            is_relevant=False,
        )
        kept = baker.make(
            MediaRelevance,
            profile=self.profile,
            location=self.location,
            source="wikimedia",
            item_key=_sha1(_FILE),
            is_relevant=True,
        )

        self._run()

        self.assertEqual(
            list(MediaRelevance.objects.filter(location=self.location).values_list("pk", "is_relevant")),
            [(kept.pk, True)],
        )

    def test_a_copied_photo_moves_to_the_clean_key(self) -> None:
        image = baker.make(
            Image,
            profile=self.profile,
            location=self.location,
            media_source_key="wikimedia",
            media_item_key=_sha1(_TRACKED),
        )

        self._run()

        image.refresh_from_db()
        self.assertEqual(image.media_item_key, media_item_key(_FILE))

    def test_a_copy_whose_item_left_the_cache_is_matched_by_the_url_it_was_fetched_from(self) -> None:
        LocationCache.objects.filter(location=self.location).delete()
        image = baker.make(
            Image,
            profile=self.profile,
            location=self.location,
            media_source_key="wikimedia",
            media_item_key=_sha1(_TRACKED),
            source_media_url=_TRACKED,
        )

        self._run()

        image.refresh_from_db()
        self.assertEqual(image.media_item_key, media_item_key(_FILE))

    def test_another_locations_marks_are_left_alone(self) -> None:
        other = baker.make(
            MediaRelevance,
            profile=self.profile,
            location=baker.make(Location),
            source="wikimedia",
            item_key=_sha1(_TRACKED),
            is_relevant=True,
        )

        self._run()

        other.refresh_from_db()
        self.assertEqual(other.item_key, _sha1(_TRACKED))
