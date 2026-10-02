"""Community wiki editing routes: album membership, overlays, layers, markup, stat votes and the article preview (P29).

Each asserts what a viewer of the wiki may do, that someone who cannot see the wiki is refused and nothing changes,
that anonymous is sent to log in, and that a malformed body is a 4xx.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki


class _WikiFixture(TestCase):
    """A wiki the user can see (they have a pin there), a contributor who hides their photos, and a stranger."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.stranger_user = baker.make(User)
        self.location = baker.make(Location, official_name="Old Mill")
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)
        self.location.ensure_slug()
        self.private_profile = baker.make(User).profile
        self.private_profile.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.private_profile.save(update_fields=["photo_upload_visibility"])
        baker.make(Pin, profile=self.private_profile, location=self.location)

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])

    def _photo(self, profile) -> Image:
        return baker.make(Image, profile=profile, wiki=self.wiki, location=self.location, pending_scan=False)

    def _post_json(self, url: str, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")


class _AlbumFixture(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.album = baker.make(Album, parent_wiki=self.wiki, profile=self.profile, name="Exteriors")
        self.mine = self._photo(self.profile)
        self.hidden = self._photo(self.private_profile)
        self.mine_item = AlbumItem.objects.create(album=self.album, image=self.mine)
        self.hidden_item = AlbumItem.objects.create(album=self.album, image=self.hidden)

    def _url(self, name: str) -> str:
        return reverse(f"location.wiki.albums.{name}", args=[self.location.slug, self.album.slug])

    def _members(self) -> set[int]:
        return set(AlbumItem.objects.filter(album=self.album).values_list("image_id", flat=True))


class WikiAlbumAddRouteTests(_AlbumFixture):
    def test_a_viewer_files_a_photo_of_this_wiki(self) -> None:
        photo = self._photo(self.profile)
        self.client.force_login(self.user)

        response = self._post_json(self._url("add"), {"image_ids": [photo.pk]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["added"], 1)
        self.assertIn(photo.pk, self._members())

    def test_a_photo_of_another_place_is_not_filed(self) -> None:
        elsewhere = baker.make(Location)
        foreign = baker.make(Image, profile=self.profile, wiki=baker.make(Wiki, location=elsewhere), location=elsewhere)
        self.client.force_login(self.user)

        response = self._post_json(self._url("add"), {"image_ids": [foreign.pk]})

        self.assertEqual(response.json()["added"], 0)
        self.assertNotIn(foreign.pk, self._members())

    def test_moving_a_photo_hidden_from_the_viewer_leaves_it_where_it_is(self) -> None:
        target = baker.make(Album, parent_wiki=self.wiki, profile=self.profile, name="Interiors")
        self.client.force_login(self.user)

        url = reverse("location.wiki.albums.add", args=[self.location.slug, target.slug])
        response = self._post_json(url, {"image_ids": [self.hidden.pk], "move_from": self.album.slug})

        self.assertNotIn("removed", response.json())
        self.assertIn(self.hidden.pk, self._members())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        photo = self._photo(self.profile)
        self.client.force_login(self.stranger_user)

        response = self._post_json(self._url("add"), {"image_ids": [photo.pk]})

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(photo.pk, self._members())

    def test_anonymous_is_redirected_to_login(self) -> None:
        photo = self._photo(self.profile)
        self.assert_login_redirect(self._post_json(self._url("add"), {"image_ids": [photo.pk]}))
        self.assertNotIn(photo.pk, self._members())

    def test_a_malformed_body_is_400(self) -> None:
        self.client.force_login(self.user)
        for body in (["x"], "x"):
            self.assertEqual(self._post_json(self._url("add"), body).status_code, 400, body)


class WikiAlbumRemoveRouteTests(_AlbumFixture):
    def test_a_viewer_removes_a_photo_they_can_see(self) -> None:
        self.client.force_login(self.user)

        response = self._post_json(self._url("remove"), {"image_ids": [self.mine.pk]})

        self.assertEqual(response.json()["removed"], 1)
        self.assertEqual(self._members(), {self.hidden.pk})
        self.assertTrue(Image.objects.filter(pk=self.mine.pk).exists(), "removing from an album deleted the photo")

    def test_a_photo_hidden_from_the_viewer_is_neither_removed_nor_confirmed(self) -> None:
        """Its uploader shows their photos to no one, so the viewer cannot have chosen it, and a count of 1 would confirm it."""
        self.client.force_login(self.user)

        response = self._post_json(self._url("remove"), {"image_ids": [self.hidden.pk]})

        self.assertEqual(response.json()["removed"], 0)
        self.assertIn(self.hidden.pk, self._members())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self._post_json(self._url("remove"), {"image_ids": [self.mine.pk]})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._members(), {self.mine.pk, self.hidden.pk})

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._post_json(self._url("remove"), {"image_ids": [self.mine.pk]}))
        self.assertEqual(self._members(), {self.mine.pk, self.hidden.pk})

    def test_a_malformed_body_is_400(self) -> None:
        self.client.force_login(self.user)
        for body in (["x"], "x"):
            self.assertEqual(self._post_json(self._url("remove"), body).status_code, 400, body)
        self.assertEqual(self._members(), {self.mine.pk, self.hidden.pk})


class WikiAlbumReorderRouteTests(_AlbumFixture):
    def setUp(self) -> None:
        super().setUp()
        self.second = self._photo(self.profile)
        self.second_item = AlbumItem.objects.create(album=self.album, image=self.second)

    def _order(self) -> list[int]:
        return list(
            AlbumItem.objects.filter(album=self.album, image__profile=self.profile)
            .exclude(order=None)
            .order_by("order")
            .values_list("pk", flat=True)
        )

    def test_a_viewer_reorders_the_album(self) -> None:
        self.client.force_login(self.user)

        response = self._post_json(self._url("reorder"), {"items": [self.second_item.pk, self.mine_item.pk]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._order(), [self.second_item.pk, self.mine_item.pk])

    def test_the_answer_does_not_count_photos_hidden_from_the_viewer(self) -> None:
        self.client.force_login(self.user)

        response = self._post_json(
            self._url("reorder"), {"items": [self.second_item.pk, self.mine_item.pk, self.hidden_item.pk]}
        )

        self.assertEqual(response.json()["reordered"], 2)

    def test_an_item_of_another_album_is_left_alone(self) -> None:
        other = baker.make(Album, parent_wiki=self.wiki, profile=self.profile, name="Interiors")
        foreign_item = AlbumItem.objects.create(album=other, image=self._photo(self.profile))
        self.client.force_login(self.user)

        self._post_json(self._url("reorder"), {"items": [foreign_item.pk, self.mine_item.pk]})

        foreign_item.refresh_from_db()
        self.assertEqual(foreign_item.album_id, other.pk)
        self.assertIsNone(foreign_item.order)

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self._post_json(self._url("reorder"), {"items": [self.second_item.pk, self.mine_item.pk]})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._order(), [])

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._post_json(self._url("reorder"), {"items": [self.second_item.pk]}))
        self.assertEqual(self._order(), [])

    def test_a_malformed_body_is_400(self) -> None:
        self.client.force_login(self.user)
        for body in (["x"], "x"):
            self.assertEqual(self._post_json(self._url("reorder"), body).status_code, 400, body)
        self.assertEqual(self._order(), [])
