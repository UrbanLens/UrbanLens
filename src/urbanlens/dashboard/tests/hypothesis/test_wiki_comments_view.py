"""Tests for WikiCommentsView (GET/POST /location/<slug>/wiki/comments/).

Covers a regression where the comment-panel context omitted ``location``,
leaving ``{% url 'location.wiki.comments' location.slug %}`` in the compose
partial resolving against an empty slug and raising NoReverseMatch.
"""
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
        other_location, _other_wiki = _location_with_wiki("Unpinned Place")
        response = self.client.get(reverse("location.wiki.comments", args=[other_location.slug]))
        self.assertEqual(response.status_code, 403)
