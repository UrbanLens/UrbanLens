"""`UL_SITE_URL` without a scheme still builds absolute links (P23).

Production's containers set `UL_SITE_URL=urbanlens.org`. Every request-less link is built as
`f"{SITE_URL}{path}"` - emails, notifications, safety alerts - so each one came out as
`urbanlens.org/safety/...`, which a mail client treats as text, and `_origin_from_url` found no origin
in it for the trusted origins.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.settings.base import _origin_from_url, _site_url_from_env

_DEFAULT = "http://localhost:21800"


class SiteUrlFromEnvTests(SimpleTestCase):
    def test_a_bare_host_is_served_over_https(self) -> None:
        self.assertEqual(_site_url_from_env("urbanlens.org", _DEFAULT), "https://urbanlens.org")

    def test_the_normalized_value_names_an_origin(self) -> None:
        self.assertEqual(_origin_from_url(_site_url_from_env("urbanlens.org", _DEFAULT)), "https://urbanlens.org")

    def test_a_url_with_a_scheme_is_kept_as_given(self) -> None:
        for url in ("https://staging.urbanlens.org", "http://localhost:21810", "https://a1b2c3.dev.urbanlens.org/"):
            with self.subTest(url=url):
                self.assertEqual(_site_url_from_env(url, _DEFAULT), url)

    def test_surrounding_whitespace_is_dropped(self) -> None:
        self.assertEqual(_site_url_from_env("  https://urbanlens.org\n", _DEFAULT), "https://urbanlens.org")

    def test_an_unset_or_blank_value_falls_back_to_the_default(self) -> None:
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(_site_url_from_env(value, _DEFAULT), _DEFAULT)
