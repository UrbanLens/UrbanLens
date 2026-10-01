"""An unearned wiki's 404 must carry exactly what a nonexistent location's does.

Production renders one template for both, but a DEBUG deployment prints the exception message, so a
message on only one branch tells a stranger which slugs are real."""

from __future__ import annotations

from django.http import Http404
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.wiki.wiki_access import get_location_or_404, resolve_visible_wiki


class UnearnedWikiMatchesMissingTests(TestCase):
    def setUp(self) -> None:
        self.request = RequestFactory().get("/")
        self.request.user = baker.make("auth.User")
        self.unearned = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=self.unearned, name="Unpinned Place")

    def _message(self, slug: str) -> str:
        with self.assertRaises(Http404) as raised:
            resolve_visible_wiki(self.request, slug)
        return str(raised.exception)

    def test_the_messages_are_identical(self) -> None:
        self.assertEqual(self._message(self.unearned.slug), self._message("no-such-location-anywhere"))

    def test_a_wikiless_location_matches_too(self) -> None:
        bare = baker.make("dashboard.Location")
        self.assertEqual(self._message(bare.slug), self._message("no-such-location-anywhere"))

    def test_the_shared_lookup_matches_a_real_unearned_wiki(self) -> None:
        """The helper other wiki-scoped controllers use to 404 before their own visibility check."""
        with self.assertRaises(Http404) as raised:
            get_location_or_404("no-such-location-anywhere")
        self.assertEqual(str(raised.exception), self._message(self.unearned.slug))
