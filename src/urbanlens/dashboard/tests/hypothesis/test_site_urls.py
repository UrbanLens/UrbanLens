"""Request-less absolute links are built in one place, on `settings.SITE_URL`."""

from __future__ import annotations

import pathlib

from django.test import override_settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core import site_urls
from urbanlens.dashboard.services.core.site_urls import absolute_url

_SRC = pathlib.Path(__file__).resolve().parents[3]


@override_settings(SITE_URL="https://urbanlens.example/")
class AbsoluteUrlTests(SimpleTestCase):
    def test_a_path_is_joined_onto_the_site(self) -> None:
        self.assertEqual(absolute_url("/safety/abc/"), "https://urbanlens.example/safety/abc/")

    def test_no_path_is_the_site_root(self) -> None:
        self.assertEqual(absolute_url(), "https://urbanlens.example")

    def test_an_absolute_url_is_refused(self) -> None:
        """Joined on, it would read as a link to this site."""
        for path in ("https://elsewhere.example/", "elsewhere.example/x", "javascript:alert(1)"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                absolute_url(path)


class NothingElseJoinsOntoSiteUrlTests(SimpleTestCase):
    """Five modules each built these links by hand; a sixth copy would miss the next rule added here."""

    def test_only_the_helper_joins_paths_onto_site_url(self) -> None:
        owner = pathlib.Path(site_urls.__file__).resolve()
        offenders = [
            str(path.relative_to(_SRC))
            for path in _SRC.rglob("*.py")
            if path.resolve() != owner
            and "/tests/" not in str(path)
            and "/migrations/" not in str(path)
            and "SITE_URL.rstrip(" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])
