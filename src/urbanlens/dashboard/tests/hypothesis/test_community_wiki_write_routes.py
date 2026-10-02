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
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer, PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote


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
        return baker.make(
            Image, profile=profile, wiki=self.wiki, location=self.location, pending_scan=False, image="photos/p.jpg"
        )

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
        return reverse(name, args=[self.location.slug, self.album.slug])

    def _members(self) -> set[int]:
        return set(AlbumItem.objects.filter(album=self.album).values_list("image_id", flat=True))


class WikiAlbumAddRouteTests(_AlbumFixture):
    def test_a_viewer_files_a_photo_of_this_wiki(self) -> None:
        photo = self._photo(self.profile)
        self.client.force_login(self.user)

        response = self._post_json(self._url("location.wiki.albums.add"), {"image_ids": [photo.pk]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["added"], 1)
        self.assertIn(photo.pk, self._members())

    def test_a_photo_of_another_place_is_not_filed(self) -> None:
        elsewhere = baker.make(Location)
        foreign = baker.make(Image, profile=self.profile, wiki=baker.make(Wiki, location=elsewhere), location=elsewhere)
        self.client.force_login(self.user)

        response = self._post_json(self._url("location.wiki.albums.add"), {"image_ids": [foreign.pk]})

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

        response = self._post_json(self._url("location.wiki.albums.add"), {"image_ids": [photo.pk]})

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(photo.pk, self._members())

    def test_anonymous_is_redirected_to_login(self) -> None:
        photo = self._photo(self.profile)
        self.assert_login_redirect(self._post_json(self._url("location.wiki.albums.add"), {"image_ids": [photo.pk]}))
        self.assertNotIn(photo.pk, self._members())

    def test_a_malformed_body_is_400(self) -> None:
        self.client.force_login(self.user)
        for body in (["x"], "x"):
            self.assertEqual(self._post_json(self._url("location.wiki.albums.add"), body).status_code, 400, body)


class WikiAlbumRemoveRouteTests(_AlbumFixture):
    def test_a_viewer_removes_a_photo_they_can_see(self) -> None:
        self.client.force_login(self.user)

        response = self._post_json(self._url("location.wiki.albums.remove"), {"image_ids": [self.mine.pk]})

        self.assertEqual(response.json()["removed"], 1)
        self.assertEqual(self._members(), {self.hidden.pk})
        self.assertTrue(Image.objects.filter(pk=self.mine.pk).exists(), "removing from an album deleted the photo")

    def test_a_photo_hidden_from_the_viewer_is_neither_removed_nor_confirmed(self) -> None:
        """Its uploader shows their photos to no one, so the viewer cannot have chosen it, and a count of 1 would confirm it."""
        self.client.force_login(self.user)

        response = self._post_json(self._url("location.wiki.albums.remove"), {"image_ids": [self.hidden.pk]})

        self.assertEqual(response.json()["removed"], 0)
        self.assertIn(self.hidden.pk, self._members())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self._post_json(self._url("location.wiki.albums.remove"), {"image_ids": [self.mine.pk]})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._members(), {self.mine.pk, self.hidden.pk})

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(
            self._post_json(self._url("location.wiki.albums.remove"), {"image_ids": [self.mine.pk]})
        )
        self.assertEqual(self._members(), {self.mine.pk, self.hidden.pk})

    def test_a_malformed_body_is_400(self) -> None:
        self.client.force_login(self.user)
        for body in (["x"], "x"):
            self.assertEqual(self._post_json(self._url("location.wiki.albums.remove"), body).status_code, 400, body)
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

        response = self._post_json(
            self._url("location.wiki.albums.reorder"), {"items": [self.second_item.pk, self.mine_item.pk]}
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._order(), [self.second_item.pk, self.mine_item.pk])

    def test_the_answer_does_not_count_photos_hidden_from_the_viewer(self) -> None:
        self.client.force_login(self.user)

        response = self._post_json(
            self._url("location.wiki.albums.reorder"),
            {"items": [self.second_item.pk, self.mine_item.pk, self.hidden_item.pk]},
        )

        self.assertEqual(response.json()["reordered"], 2)

    def test_an_item_of_another_album_is_left_alone(self) -> None:
        other = baker.make(Album, parent_wiki=self.wiki, profile=self.profile, name="Interiors")
        foreign_item = AlbumItem.objects.create(album=other, image=self._photo(self.profile))
        self.client.force_login(self.user)

        self._post_json(self._url("location.wiki.albums.reorder"), {"items": [foreign_item.pk, self.mine_item.pk]})

        foreign_item.refresh_from_db()
        self.assertEqual(foreign_item.album_id, other.pk)
        self.assertIsNone(foreign_item.order)

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self._post_json(
            self._url("location.wiki.albums.reorder"), {"items": [self.second_item.pk, self.mine_item.pk]}
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._order(), [])

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(
            self._post_json(self._url("location.wiki.albums.reorder"), {"items": [self.second_item.pk]})
        )
        self.assertEqual(self._order(), [])

    def test_a_malformed_body_is_400(self) -> None:
        self.client.force_login(self.user)
        for body in (["x"], "x"):
            self.assertEqual(self._post_json(self._url("location.wiki.albums.reorder"), body).status_code, 400, body)
        self.assertEqual(self._order(), [])


def _corners(lat: float = 42.0, lng: float = -73.0) -> list[list[float]]:
    return [[lat + 0.01, lng], [lat + 0.01, lng + 0.01], [lat, lng + 0.01], [lat, lng]]


class _OverlayFixture(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        flat = {f"{d}_{axis}": 0.0 for d in ("nw", "ne", "se", "sw") for axis in ("latitude", "longitude")}
        self.overlay = baker.make(
            MapImageOverlay,
            parent_wiki=self.wiki,
            profile=self.profile,
            name="Sanborn 1910",
            opacity=70,
            tile_url_template="/map/historical-tiles/x/{z}/{x}/{y}.png",
            **flat,
        )

    def _url(self, name: str) -> str:
        return reverse(name, args=[self.location.slug, self.overlay.uuid])


class WikiOverlayCreateRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("location.wiki.overlays", args=[self.location.slug])

    def _create(self, image: Image):
        return self.client.post(
            self.url,
            {"image_id": str(image.pk), "corners": json.dumps(_corners()), "name": "Plan"},
            HTTP_ACCEPT="application/json",
        )

    def test_a_viewer_overlays_a_photo_of_this_wiki(self) -> None:
        photo = self._photo(self.profile)
        self.client.force_login(self.user)

        response = self._create(photo)

        self.assertEqual(response.status_code, 200, response.content)
        overlay = MapImageOverlay.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(
            (overlay.parent_wiki_id, overlay.image_id, overlay.profile_id), (self.wiki.pk, photo.pk, self.profile.pk)
        )

    def test_a_photo_hidden_from_the_viewer_cannot_be_overlaid(self) -> None:
        hidden = self._photo(self.private_profile)
        self.client.force_login(self.user)

        response = self._create(hidden)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(MapImageOverlay.objects.filter(image=hidden).exists())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        photo = self._photo(self.profile)
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._create(photo).status_code, 404)
        self.assertFalse(MapImageOverlay.objects.exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._create(self._photo(self.profile)))
        self.assertFalse(MapImageOverlay.objects.exists())

    def test_malformed_corners_are_400(self) -> None:
        photo = self._photo(self.profile)
        self.client.force_login(self.user)
        for corners in ("[", "[[1,2]]", json.dumps([[91, 0]] * 4), json.dumps({"a": 1})):
            response = self.client.post(
                self.url, {"image_id": str(photo.pk), "corners": corners}, HTTP_ACCEPT="application/json"
            )
            self.assertEqual(response.status_code, 400, corners)
        self.assertFalse(MapImageOverlay.objects.exists())


class WikiOverlayEditRouteTests(_OverlayFixture):
    def test_a_viewer_renames_it_and_opacity_is_kept_in_range(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(
            self._url("location.wiki.overlays.edit"), {"name": "  Sanborn 1911 ", "opacity": "250", "order": "x"}
        )

        self.assertEqual(response.status_code, 200)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.name, "Sanborn 1911")
        self.assertLessEqual(self.overlay.opacity, 100)

    def test_a_layer_of_another_wiki_cannot_be_attached(self) -> None:
        elsewhere = baker.make(Wiki, location=baker.make(Location))
        foreign_layer = baker.make(CustomLayer, parent_wiki=elsewhere, profile=self.profile, name="Other")
        self.client.force_login(self.user)

        self.client.post(self._url("location.wiki.overlays.edit"), {"name": "x", "layer": str(foreign_layer.uuid)})

        self.overlay.refresh_from_db()
        self.assertIsNone(self.overlay.layer_id)

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(
            self.client.post(self._url("location.wiki.overlays.edit"), {"name": "Mine now"}).status_code, 404
        )
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.name, "Sanborn 1910")

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self._url("location.wiki.overlays.edit"), {"name": "Mine now"}))
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.name, "Sanborn 1910")


class WikiOverlayCornersRouteTests(_OverlayFixture):
    def _move(self):
        return self.client.post(self._url("location.wiki.overlays.corners"), {"corners": json.dumps(_corners())})

    def test_a_viewer_moves_it(self) -> None:
        self.client.force_login(self.user)

        response = self._move()

        self.assertEqual(response.status_code, 200, response.content)
        self.overlay.refresh_from_db()
        self.assertAlmostEqual(self.overlay.sw_latitude, 42.0)

    def test_a_locked_overlay_does_not_move(self) -> None:
        MapImageOverlay.objects.filter(pk=self.overlay.pk).update(locked=True)
        self.client.force_login(self.user)

        self.assertEqual(self._move().status_code, 409)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.sw_latitude, 0.0)

    def test_malformed_corners_are_400(self) -> None:
        self.client.force_login(self.user)
        for corners in ("", "[", json.dumps([[0, 181]] * 4), json.dumps([["a", "b"]] * 4)):
            self.assertEqual(
                self.client.post(self._url("location.wiki.overlays.corners"), {"corners": corners}).status_code,
                400,
                corners,
            )
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.sw_latitude, 0.0)

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._move().status_code, 404)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.sw_latitude, 0.0)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._move())
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.sw_latitude, 0.0)


class WikiLayerReorderRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.first = baker.make(CustomLayer, parent_wiki=self.wiki, profile=self.profile, name="Fences", order=0)
        self.second = baker.make(CustomLayer, parent_wiki=self.wiki, profile=self.profile, name="Holes", order=1)

    def _reorder(self, layer: CustomLayer, direction: str):
        url = reverse("location.wiki.layers.reorder", args=[self.location.slug, layer.uuid])
        return self.client.post(url, {"direction": direction})

    def _orders(self) -> tuple[int, int]:
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        return self.first.order, self.second.order

    def test_a_viewer_moves_a_layer_down(self) -> None:
        self.client.force_login(self.user)

        self.assertEqual(self._reorder(self.first, "down").status_code, 200)
        self.assertEqual(self._orders(), (1, 0))

    def test_past_either_end_or_an_unknown_direction_changes_nothing(self) -> None:
        self.client.force_login(self.user)

        for layer, direction in ((self.first, "up"), (self.second, "down"), (self.first, "sideways")):
            self.assertEqual(self._reorder(layer, direction).status_code, 200)
        self.assertEqual(self._orders(), (0, 1))

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._reorder(self.first, "down").status_code, 404)
        self.assertEqual(self._orders(), (0, 1))

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._reorder(self.first, "down"))
        self.assertEqual(self._orders(), (0, 1))


class WikiStatVoteRouteTests(_WikiFixture):
    def _vote(self, value, field: str = "danger"):
        url = reverse("location.wiki.stat_vote", args=[self.location.slug, field])
        return self.client.post(url, {} if value is None else {"value": value})

    def _my_vote(self) -> int | None:
        vote = WikiStatVote.objects.filter(wiki=self.wiki, profile=self.profile, field="danger").first()
        return vote.value if vote else None

    def test_a_viewer_casts_changes_and_clears_a_vote(self) -> None:
        self.client.force_login(self.user)

        self.assertEqual(self._vote(4).status_code, 200)
        self.assertEqual(self._my_vote(), 4)
        self._vote(2)
        self.assertEqual(self._my_vote(), 2)
        self._vote(0)
        self.assertIsNone(self._my_vote())

    def test_a_malformed_value_is_400_and_keeps_the_vote(self) -> None:
        """The stars only ever send 0 to 5, so anything else is a garbled request, not a request to clear."""
        self.client.force_login(self.user)
        self._vote(3)

        for value in ("abc", "9", "-1", "2.5"):
            self.assertEqual(self._vote(value).status_code, 400, value)
        self.assertEqual(self._my_vote(), 3)

    def test_an_unknown_field_is_404(self) -> None:
        self.client.force_login(self.user)
        self.assertEqual(self._vote(3, field="beauty").status_code, 404)

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._vote(5).status_code, 404)
        self.assertFalse(WikiStatVote.objects.exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._vote(5))
        self.assertFalse(WikiStatVote.objects.exists())


class WikiArticlePreviewRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("location.wiki.article.preview", args=[self.location.slug])

    def test_a_viewer_sees_it_rendered_and_nothing_is_saved(self) -> None:
        from urbanlens.dashboard.models.article.model import Article

        self.client.force_login(self.user)

        response = self.client.post(self.url, {"content": "**Boiler house** <script>alert(1)</script>"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<strong>Boiler house</strong>")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertFalse(Article.objects.exists())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)
        self.assertEqual(self.client.post(self.url, {"content": "x"}).status_code, 404)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"content": "x"}))


class WikiMarkupCreateRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("location.wiki.markup", args=[self.location.slug])
        self.line = {
            "markup_type": "line",
            "geometry": {"type": "LineString", "coordinates": [[-73, 42], [-73.001, 42.001]]},
        }

    def test_a_viewer_draws_on_the_wiki_map(self) -> None:
        self.client.force_login(self.user)

        response = self._post_json(self.url, self.line)

        self.assertEqual(response.status_code, 200, response.content)
        markup = PinMarkup.objects.get(uuid=response.json()["uuid"])
        self.assertEqual((markup.parent_wiki_id, markup.profile_id), (self.wiki.pk, self.profile.pk))

    def test_malformed_markup_is_400(self) -> None:
        self.client.force_login(self.user)
        bodies = (
            ["x"],
            {"markup_type": "spiral", "geometry": self.line["geometry"]},
            {"markup_type": "line"},
            {"markup_type": "line", "geometry": {"type": "Point", "coordinates": [-73, 42]}},
        )
        for body in bodies:
            self.assertEqual(self._post_json(self.url, body).status_code, 400, body)
        self.assertFalse(PinMarkup.objects.exists())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._post_json(self.url, self.line).status_code, 404)
        self.assertFalse(PinMarkup.objects.exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._post_json(self.url, self.line))
        self.assertFalse(PinMarkup.objects.exists())
