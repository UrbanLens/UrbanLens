"""Tests for WikiCommentsView (GET/POST /location/<slug>/wiki/comments/)."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


def _location_with_wiki(name: str = "Old Mill"):
    location = baker.make("dashboard.Location")
    wiki = baker.make("dashboard.Wiki", location=location, name=name)
    return location, wiki


class WikiCommentsViewTests(TestCase):
    """GET/POST /location/<slug>/wiki/comments/"""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.location, self.wiki = _location_with_wiki()
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def _url(self):
        return reverse("location.wiki.comments", args=[self.location.slug])

    def test_get_renders_without_error(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_get_context_includes_location_for_compose_partial(self):
        response = self.client.get(self._url())
        self.assertEqual(response.context["location"], self.location)

    def test_post_creates_comment_and_context_includes_location(self):
        response = self.client.post(self._url(), {"text": "Great spot!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["location"], self.location)
        self.assertTrue(self.wiki.comments.filter(text="Great spot!").exists())

    def test_forbidden_without_a_pin_at_this_location(self):
        # 404, not 403: resolve_visible_wiki makes an unpinned location's wiki
        # indistinguishable from one that doesn't exist (see wiki_access.py).
        other_location, _other_wiki = _location_with_wiki("Unpinned Place")
        response = self.client.get(reverse("location.wiki.comments", args=[other_location.slug]))
        self.assertEqual(response.status_code, 404)


class WikiChildCommentsSurviveAPostTests(TestCase):
    """A comment posted while the wiki page shows child wikis' comments keeps them in the re-rendered panel."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.location, self.wiki = _location_with_wiki("Campus")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)
        child_location = baker.make("dashboard.Location")
        child = baker.make("dashboard.Wiki", location=child_location, name="Powerhouse", parent_wiki=self.wiki)
        baker.make("dashboard.Comment", wiki=child, pin=None, profile=self.profile, text="a comment on the child wiki")

    def test_posting_keeps_the_child_wikis_comments(self):
        page = reverse("location.wiki", args=[self.location.slug]) + "?children=1"

        response = self.client.post(
            reverse("location.wiki.comments", args=[self.location.slug]),
            {"text": "a comment on the campus"},
            HTTP_HX_REQUEST="true",
            HTTP_HX_CURRENT_URL=f"http://testserver{page}",
        )

        self.assertContains(response, "a comment on the campus")
        self.assertContains(response, "a comment on the child wiki")
