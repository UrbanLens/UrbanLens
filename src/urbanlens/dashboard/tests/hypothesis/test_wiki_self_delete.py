"""Tests for a wiki creator's self-service delete window.

Covers Wiki.can_be_deleted_by, the view-tracking that retires it (the first
view by anyone other than the creator, via LocationWikiView.get), and
LocationWikiDeleteView which acts on it.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.wiki.model import Wiki


def _location_with_wiki(creator, name: str = "Old Mill"):
    location = baker.make("dashboard.Location")
    wiki = baker.make("dashboard.Wiki", location=location, name=name, created_by=creator, viewed_by_other=False)
    return location, wiki


class CanBeDeletedByTests(TestCase):
    """Wiki.can_be_deleted_by gates on creator identity and viewed_by_other."""

    def test_creator_can_delete_before_anyone_views(self) -> None:
        creator = baker.make("auth.User").profile
        _location, wiki = _location_with_wiki(creator)
        self.assertTrue(wiki.can_be_deleted_by(creator))

    def test_non_creator_cannot_delete(self) -> None:
        creator = baker.make("auth.User").profile
        other = baker.make("auth.User").profile
        _location, wiki = _location_with_wiki(creator)
        self.assertFalse(wiki.can_be_deleted_by(other))

    def test_creator_cannot_delete_once_viewed_by_other(self) -> None:
        creator = baker.make("auth.User").profile
        _location, wiki = _location_with_wiki(creator)
        wiki.viewed_by_other = True
        self.assertFalse(wiki.can_be_deleted_by(creator))

    def test_wiki_with_no_creator_is_never_self_deletable(self) -> None:
        someone = baker.make("auth.User").profile
        location = baker.make("dashboard.Location")
        wiki = baker.make("dashboard.Wiki", location=location, name="Legacy", created_by=None)
        self.assertFalse(wiki.can_be_deleted_by(someone))


class ViewTrackingTests(TestCase):
    """LocationWikiView.get flips viewed_by_other on the first non-creator view."""

    def test_creators_own_view_does_not_flip_the_flag(self) -> None:
        creator_user = baker.make("auth.User")
        creator = creator_user.profile
        location, wiki = _location_with_wiki(creator)

        self.client.force_login(creator_user)
        response = self.client.get(reverse("location.wiki", args=[location.slug]))

        self.assertEqual(response.status_code, 200)
        wiki.refresh_from_db()
        self.assertFalse(wiki.viewed_by_other)

    def test_other_users_view_flips_the_flag(self) -> None:
        creator = baker.make("auth.User").profile
        viewer_user = baker.make("auth.User")
        location, wiki = _location_with_wiki(creator)

        self.client.force_login(viewer_user)
        response = self.client.get(reverse("location.wiki", args=[location.slug]))

        self.assertEqual(response.status_code, 200)
        wiki.refresh_from_db()
        self.assertTrue(wiki.viewed_by_other)

    def test_delete_button_shown_only_while_eligible(self) -> None:
        creator_user = baker.make("auth.User")
        creator = creator_user.profile
        location, _wiki = _location_with_wiki(creator)

        self.client.force_login(creator_user)
        response = self.client.get(reverse("location.wiki", args=[location.slug]))
        self.assertContains(response, reverse("location.wiki.delete", args=[location.slug]))


class LocationWikiDeleteViewTests(TestCase):
    """DELETE /location/<slug>/wiki/delete/"""

    def setUp(self):
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile

    def _delete(self, location_slug: str):
        return self.client.delete(reverse("location.wiki.delete", args=[location_slug]))

    def test_creator_deletes_unviewed_wiki(self) -> None:
        location, wiki = _location_with_wiki(self.creator)
        self.client.force_login(self.creator_user)

        response = self._delete(location.slug)

        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Redirect", response)
        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_non_creator_cannot_delete(self) -> None:
        location, wiki = _location_with_wiki(self.creator)
        other_user = baker.make("auth.User")
        self.client.force_login(other_user)

        response = self._delete(location.slug)

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_creator_cannot_delete_after_someone_else_viewed(self) -> None:
        location, wiki = _location_with_wiki(self.creator)
        other_user = baker.make("auth.User")
        self.client.force_login(other_user)
        self.client.get(reverse("location.wiki", args=[location.slug]))

        self.client.force_login(self.creator_user)
        response = self._delete(location.slug)

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_delete_cascades_to_child_wikis(self) -> None:
        location, wiki = _location_with_wiki(self.creator)
        child_location = baker.make("dashboard.Location")
        child = baker.make("dashboard.Wiki", location=child_location, name="Basement", parent_wiki=wiki)
        self.client.force_login(self.creator_user)

        response = self._delete(location.slug)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())
        self.assertFalse(Wiki.objects.filter(pk=child.pk).exists())
