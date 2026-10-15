"""A URL's tracking parameters never change what it identifies."""

from __future__ import annotations

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.images.relevance import media_item_key
from urbanlens.dashboard.services.core.tracking_params import without_tracking_params

_FILE = "https://upload.wikimedia.org/wikipedia/commons/6/68/Cheney_bldg.jpg"
_TRACKED = f"{_FILE}?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=original"


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
