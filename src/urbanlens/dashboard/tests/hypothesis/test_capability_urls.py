"""Which URLs work as a password, and so are never handed to a service that publishes what it fetches."""

from __future__ import annotations

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.security.capability_urls import SHARE_DOMAINS, is_capability_url

_PLAIN_URLS = st.sampled_from(
    (
        "https://example.com/",
        "https://example.com/article/hudson-river-state-hospital",
        "https://news.example.org/2024/05/demolition?page=2&ref=rss",
        "https://www.google.com/maps/place/Hudson+River+State+Hospital/@41.73,-73.92,17z",
        "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital#History",
    )
)
_CREDENTIAL_NAMES = st.sampled_from(
    ("token", "access_token", "key", "api-key", "X-Amz-Signature", "sig", "auth", "code", "passcode", "resourcekey")
)


class IsCapabilityUrlTests(SimpleTestCase):
    @given(_PLAIN_URLS)
    def test_a_page_without_a_secret_is_not_one(self, url: str) -> None:
        self.assertFalse(is_capability_url(url))

    @given(_PLAIN_URLS, _CREDENTIAL_NAMES, st.booleans())
    def test_a_credential_parameter_makes_any_url_one(self, url: str, name: str, in_fragment: bool) -> None:
        url = url.split("#", 1)[0]
        separator = "#" if in_fragment else ("&" if "?" in url else "?")

        self.assertTrue(is_capability_url(f"{url}{separator}{name}=abc123"))

    @given(st.sampled_from(sorted(SHARE_DOMAINS)), st.sampled_from(("", "www.", "dl.", "a.b.")))
    def test_a_share_host_or_its_subdomain_is_one(self, domain: str, prefix: str) -> None:
        self.assertTrue(is_capability_url(f"https://{prefix}{domain}/anything"))

    def test_a_host_only_resembling_a_share_host_is_not_one(self) -> None:
        for url in (
            "https://notdropbox.com/plans.pdf",
            "https://dropbox.com.example.org/plans.pdf",
            "https://example.com/dropbox.com/plans.pdf",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_capability_url(url))

    def test_google_my_maps_and_saved_lists_are_one_on_any_google_domain(self) -> None:
        for url in (
            "https://www.google.com/maps/d/viewer?mid=1AbC",
            "https://www.google.co.uk/maps/d/u/0/edit?mid=1AbC",
            "https://google.de/maps/placelists/list/AbCdEf",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_capability_url(url))

    def test_a_share_path_on_any_host_is_one(self) -> None:
        for url in (
            "https://cloud.example.com/index.php/s/AbCdEfGhIjKl",
            "https://photos.example.com/share/AbCdEfGhIjKlMnOp",
            "https://www.reddit.com/r/urbanexploration/s/AbCdEfGh",
            "https://www.flickr.com/gp/12345678@N00/AbC123",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_capability_url(url))

    def test_a_short_word_after_a_share_segment_is_not_a_key(self) -> None:
        self.assertFalse(is_capability_url("https://example.com/share/news"))

    def test_a_user_part_is_one(self) -> None:
        self.assertTrue(is_capability_url("https://alice:secret@example.com/"))
