"""Access-control tests for wiki-scoped controllers and comment-author privacy.

Covers two regressions:

- Wiki-scoped views (page, gallery, boundary, aliases, badge membership,
  markup, detail pins, comments) resolved the Location/Wiki from the URL
  slug alone, so any logged-in user could view or edit the wiki for a place
  they had never pinned. Every one of them must instead resolve through
  ``resolve_visible_wiki``/``location_visible_to``, which requires the
  requester to have a pin at that Location.
- Comment visibility (and reactions) must also respect the comment author's
  own ``comment_visibility`` privacy setting, independent of whether the
  viewer can see the page the comment is on.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.badges.meta import KIND_TAG
from urbanlens.dashboard.models.badges.model import Badge
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki


class WikiVisibilityTests(TestCase):
    """Only a profile with a pin at the Location may access its wiki, anywhere."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.other = baker.make(User)
        self.client.force_login(self.user)
        self.other_pin = baker.make(Pin, profile=self.other.profile)
        self.wiki = baker.make(Wiki, location=self.other_pin.location)
        self.own_pin = baker.make(Pin, profile=self.user.profile)
        self.own_wiki = baker.make(Wiki, location=self.own_pin.location)

    def test_wiki_page_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_page_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_gallery_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.gallery", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_gallery_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.gallery", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_boundary_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.boundary", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_boundary_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.boundary", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_aliases_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.aliases", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_aliases_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.aliases", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_badge_membership_unpinned_404s(self) -> None:
        response = self.client.get(reverse("badge.location", kwargs={"badge_kind": "tag", "location_slug": self.wiki.location.slug}))
        self.assertEqual(response.status_code, 404)

    def test_wiki_badge_membership_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("badge.location", kwargs={"badge_kind": "tag", "location_slug": self.own_wiki.location.slug}))
        self.assertEqual(response.status_code, 200)

    def test_wiki_markup_json_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.markup.json", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_markup_json_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.markup.json", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_detail_pins_json_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.detail_pins.json", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_detail_pins_json_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.detail_pins.json", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_detail_pins_panel_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.detail_pins.panel", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_detail_pins_panel_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.detail_pins.panel", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_comments_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.comments", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_comments_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.comments", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_wiki_history_unpinned_404s(self) -> None:
        response = self.client.get(reverse("location.wiki.history", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_history_pinned_succeeds(self) -> None:
        response = self.client.get(reverse("location.wiki.history", args=[self.own_wiki.location.slug]))
        self.assertEqual(response.status_code, 200)


class CommentAuthorPrivacyTests(TestCase):
    """A comment author's ``comment_visibility`` setting gates reading/reacting, not just page access."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.author = baker.make(User)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.user.profile)
        self.wiki = baker.make(Wiki, location=self.pin.location)
        # Author also has a pin here, so page-level (location) visibility
        # passes for both profiles - only comment_visibility should differ.
        baker.make(Pin, profile=self.author.profile, location=self.pin.location)

    def _comment(self, *, visibility: str) -> Comment:
        self.author.profile.comment_visibility = visibility
        self.author.profile.save(update_fields=["comment_visibility"])
        return baker.make(Comment, pin=None, wiki=self.wiki, profile=self.author.profile)

    def test_can_react_when_author_allows_anyone(self) -> None:
        comment = self._comment(visibility=VisibilityChoice.ANYONE)
        response = self.client.post(reverse("comment.react", args=[comment.id]), data={"emoji": "👍"})
        self.assertEqual(response.status_code, 200)

    def test_cannot_react_when_author_restricts_to_no_one(self) -> None:
        comment = self._comment(visibility=VisibilityChoice.NO_ONE)
        response = self.client.post(reverse("comment.react", args=[comment.id]), data={"emoji": "👍"})
        self.assertEqual(response.status_code, 404)

    def test_cannot_react_when_author_restricts_to_friends(self) -> None:
        comment = self._comment(visibility=VisibilityChoice.FRIENDS)
        response = self.client.post(reverse("comment.react", args=[comment.id]), data={"emoji": "👍"})
        self.assertEqual(response.status_code, 404)

    def test_restricted_comment_is_excluded_from_wiki_comment_listing(self) -> None:
        comment = self._comment(visibility=VisibilityChoice.NO_ONE)
        response = self.client.get(reverse("location.wiki.comments", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 200)
        rendered_comment_ids = {row["comment"].id for row in response.context["rendered_comments"]}
        self.assertNotIn(comment.id, rendered_comment_ids)

    def test_visible_comment_is_included_in_wiki_comment_listing(self) -> None:
        comment = self._comment(visibility=VisibilityChoice.ANYONE)
        response = self.client.get(reverse("location.wiki.comments", args=[self.wiki.location.slug]))
        self.assertEqual(response.status_code, 200)
        rendered_comment_ids = {row["comment"].id for row in response.context["rendered_comments"]}
        self.assertIn(comment.id, rendered_comment_ids)

    def test_own_comment_always_visible_regardless_of_setting(self) -> None:
        self.user.profile.comment_visibility = VisibilityChoice.NO_ONE
        self.user.profile.save(update_fields=["comment_visibility"])
        own_comment = baker.make(Comment, pin=None, wiki=self.wiki, profile=self.user.profile)
        response = self.client.post(reverse("comment.react", args=[own_comment.id]), data={"emoji": "👍"})
        self.assertEqual(response.status_code, 200)
