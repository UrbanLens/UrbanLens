"""The Photos tab's behaviour: album date ranges, deep-linking, and uploading.

These cover the parts of the album UI that are decided server-side, so the
client can stay thin:

* an album's date range comes from its photos' capture times, not upload times;
* an opened album is addressable as ``?album=<slug>``, which is what makes the
  browser's Back button work and what a pasted link has to resolve;
* uploading into an album goes through the same gates the pin/wiki galleries
  use, and files the new photo in one step.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from http import HTTPStatus
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.albums import _photo_map_payload
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.photos.albums import album_date_range

_PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _pin_with_album(name: str = "Interior"):
    """A pin, its owner logged out, and one album on it."""
    pin = baker.make_recipe("dashboard.pin")
    album = Album.objects.create(name=name, profile=pin.profile, parent_pin=pin)
    return pin, album


class AlbumDateRangeTests(TestCase):
    """The range spans capture time, falling back to upload time."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")

    def _photo(self, taken_at: datetime | None = None) -> Image:
        return baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile, taken_at=taken_at)

    def test_an_empty_album_has_no_range(self) -> None:
        self.assertEqual(album_date_range([]), (None, None))

    def test_the_range_spans_the_earliest_and_latest_capture(self) -> None:
        early = self._photo(datetime(2019, 5, 2, 12, 0, tzinfo=UTC))
        late = self._photo(datetime(2021, 8, 14, 9, 30, tzinfo=UTC))
        middle = self._photo(datetime(2020, 1, 1, 0, 0, tzinfo=UTC))

        first, last = album_date_range([late, middle, early])

        self.assertEqual(first, early.taken_at)
        self.assertEqual(last, late.taken_at)

    def test_capture_time_wins_over_upload_time(self) -> None:
        """A photo uploaded today but taken in 2019 dates the album to 2019."""
        photo = self._photo(datetime(2019, 5, 2, 12, 0, tzinfo=UTC))

        first, last = album_date_range([photo])

        self.assertEqual(first, photo.taken_at)
        self.assertEqual(last, photo.taken_at)
        self.assertNotEqual(first, photo.created)

    def test_a_photo_without_exif_falls_back_to_when_it_was_uploaded(self) -> None:
        """Dropping undated photos would make the range narrower than the album."""
        undated = self._photo(None)

        first, last = album_date_range([undated])

        self.assertEqual(first, undated.created)
        self.assertEqual(last, undated.created)

    def test_a_mixed_album_considers_both_kinds_of_date(self) -> None:
        dated = self._photo(datetime(2019, 5, 2, 12, 0, tzinfo=UTC))
        undated = self._photo(None)

        first, last = album_date_range([dated, undated])

        self.assertEqual(first, dated.taken_at)
        self.assertEqual(last, undated.created)


class AlbumDeepLinkTests(TestCase):
    """``?album=<slug>`` addresses one album, so Back and shared links work."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums", args=[self.pin.slug])

    def test_without_the_parameter_the_album_list_renders(self) -> None:
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertTemplateUsed(response, "dashboard/partials/albums/_albums_panel.html")

    def test_the_parameter_renders_that_album(self) -> None:
        response = self.client.get(self.url, {"album": self.album.slug})

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertTemplateUsed(response, "dashboard/partials/albums/_album_detail.html")
        self.assertContains(response, f'data-album-slug="{self.album.slug}"')

    def test_an_unknown_album_falls_back_to_the_list(self) -> None:
        """A bookmark to a deleted album should still land somewhere useful."""
        response = self.client.get(self.url, {"album": "no-such-album"})

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertTemplateUsed(response, "dashboard/partials/albums/_albums_panel.html")

    def test_another_pins_album_is_not_reachable(self) -> None:
        """The slug is resolved through this owner's albums, not globally."""
        _other_pin, other_album = _pin_with_album(name="Someone else's")

        response = self.client.get(self.url, {"album": other_album.slug})

        self.assertTemplateUsed(response, "dashboard/partials/albums/_albums_panel.html")

    def test_the_detail_view_carries_the_list_url_for_history_restore(self) -> None:
        """album-items.ts rebuilds the list URL from this attribute on Back."""
        response = self.client.get(self.url, {"album": self.album.slug})

        self.assertContains(response, f'data-list-url="{self.url}"')


class AlbumPanelSectionsTests(TestCase):
    """What the album list shows, and what it hides."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums", args=[self.pin.slug])

    def test_the_loose_section_is_hidden_when_every_photo_is_filed(self) -> None:
        image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        AlbumItem.objects.create(album=self.album, image=image, order=0)

        response = self.client.get(self.url)

        self.assertNotContains(response, "Not in an album")
        self.assertNotContains(response, "albums-loose-grid")

    def test_the_loose_section_shows_when_something_is_unfiled(self) -> None:
        baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)

        response = self.client.get(self.url)

        self.assertContains(response, "Not in an album")

    def test_the_loose_section_is_hidden_when_there_are_no_photos_at_all(self) -> None:
        response = self.client.get(self.url)

        self.assertNotContains(response, "albums-loose-grid")

    def test_the_create_form_starts_hidden(self) -> None:
        """It is revealed by the "New album" button, not shown to everyone always."""
        response = self.client.get(self.url)

        self.assertContains(response, 'id="album-create-form"')
        self.assertContains(response, "data-album-create-toggle")
        self.assertRegex(response.content.decode(), r'id="album-create-form"[^>]*\shidden')

    def test_the_list_offers_drop_targets_and_a_bulk_toolbar(self) -> None:
        response = self.client.get(self.url)

        self.assertContains(response, "data-album-drop")
        self.assertContains(response, 'id="ul-bulk-bar-albums"')
        self.assertContains(response, "data-album-select-toggle")
        self.assertContains(response, "album-target-dialog")

    def test_the_type_explainer_is_collapsible(self) -> None:
        response = self.client.get(self.url)

        self.assertContains(response, "album-kind-explainer")
        self.assertContains(response, "Shots of the same scene from the same angle")


class AlbumUploadViewTests(TestCase):
    """Uploading straight into an album."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums.upload", args=[self.pin.slug, self.album.slug])
        self.enterContext(patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"))

    def _upload(self, name: str = "shot.png", content: bytes = _PNG_BYTES):
        return self.client.post(self.url, {"image": SimpleUploadedFile(name, content, content_type="image/png")})

    def test_a_missing_file_is_a_400(self) -> None:
        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(response.json()["error"], "No image provided.")

    def test_a_valid_upload_lands_in_the_album(self) -> None:
        response = self._upload()

        self.assertEqual(response.status_code, HTTPStatus.CREATED, response.content)
        image = Image.objects.get(profile=self.pin.profile)
        self.assertEqual(image.pin, self.pin)
        self.assertTrue(AlbumItem.objects.filter(album=self.album, image=image).exists())

    def test_the_response_is_the_shared_gallery_json(self) -> None:
        """The client renders the new tile with the same code the galleries use."""
        response = self._upload()

        body = response.json()
        self.assertEqual(body["id"], Image.objects.get(profile=self.pin.profile).pk)
        self.assertIn("url", body)

    def test_a_duplicate_is_filed_in_the_album_instead_of_refused(self) -> None:
        """Dropping a file that's already on the pin adds the existing photo, as success."""
        first = self._upload()
        self.assertEqual(first.status_code, HTTPStatus.CREATED, first.content)
        image = Image.objects.get(profile=self.pin.profile)
        AlbumItem.objects.filter(album=self.album, image=image).delete()

        response = self._upload(name="same-bytes.png")

        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        self.assertEqual(Image.objects.filter(profile=self.pin.profile).count(), 1)
        self.assertTrue(AlbumItem.objects.filter(album=self.album, image=image).exists())
        self.assertEqual(response.json()["id"], image.pk)

    def test_reuploading_a_photo_already_in_the_album_is_still_success(self) -> None:
        self._upload()

        response = self._upload(name="same-bytes.png")

        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        self.assertEqual(Image.objects.filter(profile=self.pin.profile).count(), 1)
        self.assertEqual(AlbumItem.objects.filter(album=self.album).count(), 1)

    @patch(
        "urbanlens.dashboard.services.photos.uploads.image_upload_error",
        new=MagicMock(return_value=("Not an image.", 415)),
    )
    def test_a_rejected_file_creates_nothing(self) -> None:
        """The album upload goes through the same gate as the galleries.

        Patched rather than fed a bad file so this asserts the wiring - that a
        refusal from the shared check reaches the client and leaves no row -
        rather than re-testing the sniffing rules themselves.
        """
        response = self._upload()

        self.assertEqual(response.status_code, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        self.assertEqual(response.json()["error"], "Not an image.")
        self.assertFalse(Image.objects.filter(profile=self.pin.profile).exists())
        self.assertFalse(AlbumItem.objects.filter(album=self.album).exists())

    def test_another_users_album_is_not_writable(self) -> None:
        _other_pin, other_album = _pin_with_album(name="Not yours")
        url = reverse("pin.albums.upload", args=[self.pin.slug, other_album.slug])

        response = self.client.post(url, {"image": SimpleUploadedFile("shot.png", _PNG_BYTES, content_type="image/png")})

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)


class AlbumMapPayloadTests(TestCase):
    """The album map only offers a drag the server would actually accept.

    Exercised at the payload level: whether a photo is *visible* to a viewer is
    already settled by ``album_images``, and mixing that in here would test the
    visibility gate rather than the map's own rules.
    """

    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make_recipe("dashboard.pin")

    def _photo(self, profile, **kwargs) -> Image:
        return baker.make_recipe("dashboard.image", pin=self.pin, profile=profile, **kwargs)

    def test_your_own_photo_is_movable(self) -> None:
        photo = self._photo(self.pin.profile, latitude="39.5", longitude="-75.5")

        payload = _photo_map_payload([photo], self.pin.profile)

        self.assertEqual(len(payload), 1)
        self.assertTrue(payload[0]["movable"])
        self.assertTrue(payload[0]["placed"])

    def test_someone_elses_photo_is_shown_but_not_movable(self) -> None:
        """Repositioning refuses another profile's photo, so don't offer the drag."""
        other = baker.make_recipe("dashboard.pin")
        photo = self._photo(other.profile, latitude="39.5", longitude="-75.5")

        payload = _photo_map_payload([photo], self.pin.profile)

        self.assertEqual(len(payload), 1)
        self.assertFalse(payload[0]["movable"])

    def test_a_photo_with_no_gps_sits_at_the_place_and_is_flagged_unplaced(self) -> None:
        """It still appears, so the user can drag it to where it was actually taken.

        Every uploaded photo carries its place's Location (see
        services.photos.uploads), which is where an EXIF-less photo lands.
        """
        photo = self._photo(self.pin.profile, location=self.pin.location, latitude=None, longitude=None)

        payload = _photo_map_payload([photo], self.pin.profile)

        self.assertEqual(len(payload), 1)
        self.assertFalse(payload[0]["placed"])
        self.assertEqual(payload[0]["lat"], float(self.pin.location.latitude))

    def test_a_photo_with_no_position_anywhere_is_left_off(self) -> None:
        """Nothing to show and nowhere to put it - a marker at 0,0 would be a lie."""
        photo = self._photo(self.pin.profile, location=None, latitude=None, longitude=None)

        self.assertEqual(_photo_map_payload([photo], self.pin.profile), [])

    def test_an_anonymous_viewer_can_move_nothing(self) -> None:
        photo = self._photo(self.pin.profile, latitude="39.5", longitude="-75.5")

        payload = _photo_map_payload([photo], None)

        self.assertFalse(payload[0]["movable"])

    def test_a_map_hidden_photo_is_left_off_even_with_gps(self) -> None:
        """map_hidden opts a photo out of the map entirely, not just out of the drag."""
        photo = self._photo(self.pin.profile, latitude="39.5", longitude="-75.5", map_hidden=True)

        self.assertEqual(_photo_map_payload([photo], self.pin.profile), [])


class AlbumMapRenderTests(TestCase):
    """The album view ships the map payload to the client."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums", args=[self.pin.slug])

    def test_the_payload_is_rendered_as_json_for_the_map(self) -> None:
        image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile, latitude="39.5", longitude="-75.5")
        AlbumItem.objects.create(album=self.album, image=image, order=0)

        response = self.client.get(self.url, {"album": self.album.slug})

        self.assertContains(response, 'id="album-map-photos"')
        self.assertContains(response, '"movable": true')
        self.assertContains(response, 'data-reposition-base="/dashboard/map/pin/')


class AlbumItemsPageTests(TestCase):
    """The album grid is paged so a large album does not dump every file URL at once."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums.items", args=[self.pin.slug, self.album.slug])

    def test_an_empty_album_returns_an_empty_page(self) -> None:
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        body = response.json()
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"], 0)

    def test_the_page_carries_thumb_and_lightbox_fields(self) -> None:
        image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        AlbumItem.objects.create(album=self.album, image=image, order=0)

        response = self.client.get(self.url)

        item = response.json()["items"][0]
        self.assertEqual(item["id"], image.pk)
        self.assertEqual(item["uuid"], str(image.uuid))
        self.assertIn("thumb_url", item)
        self.assertIn("url", item)

    def test_paging_does_not_enqueue_thumbnail_generation(self) -> None:
        """Thumbnails are written after upload (and backfilled on a schedule), not on view."""
        image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        AlbumItem.objects.create(album=self.album, image=image, order=0)

        with patch("urbanlens.dashboard.controllers.albums.safely_enqueue_task") as enqueue:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, HTTPStatus.OK)
        enqueue.assert_not_called()

    def test_offset_skips_earlier_photos(self) -> None:
        photos = [baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile) for _ in range(3)]
        for i, photo in enumerate(photos):
            AlbumItem.objects.create(album=self.album, image=photo, order=i)

        response = self.client.get(self.url, {"offset": 1, "limit": 1})

        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["items"]), 1)

    def test_limit_and_offset_are_clamped_to_sane_bounds(self) -> None:
        """A wild limit can't turn the page into a full album dump, nor a negative offset wrap around."""
        response = self.client.get(self.url, {"offset": -5, "limit": 99999})

        body = response.json()
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["limit"], 100)

        response = self.client.get(self.url, {"limit": 0})

        self.assertEqual(response.json()["limit"], 1)


class AlbumMoveTests(TestCase):
    """Adding with move_from removes the photos from the source album."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.source = _pin_with_album("Interior")
        self.target = Album.objects.create(name="Exterior", profile=self.pin.profile, parent_pin=self.pin)
        self.client.force_login(self.pin.profile.user)
        self.photo = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        AlbumItem.objects.create(album=self.source, image=self.photo, order=0)
        self.url = reverse("pin.albums.add", args=[self.pin.slug, self.target.slug])

    def test_move_files_the_photo_in_the_target_and_clears_the_source(self) -> None:
        response = self.client.post(
            self.url,
            data={"image_ids": [self.photo.pk], "move_from": self.source.slug},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.json()["added"], 1)
        self.assertEqual(response.json()["removed"], 1)
        self.assertTrue(AlbumItem.objects.filter(album=self.target, image=self.photo).exists())
        self.assertFalse(AlbumItem.objects.filter(album=self.source, image=self.photo).exists())

    def test_move_from_matching_the_target_itself_is_ignored(self) -> None:
        """A same-album ``move_from`` must not undo the very add it accompanies."""
        photo = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)

        response = self.client.post(
            self.url,
            data={"image_ids": [photo.pk], "move_from": self.target.slug},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(response.json()["added"], 1)
        self.assertNotIn("removed", response.json())
        self.assertTrue(AlbumItem.objects.filter(album=self.target, image=photo).exists())


class AlbumPickerJsonTests(TestCase):
    """The add-to-album dialog lists this owner's albums as JSON."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums", args=[self.pin.slug])

    def test_picker_lists_the_album_name_and_add_url(self) -> None:
        response = self.client.get(self.url, {"picker": "1"})

        self.assertEqual(response.status_code, HTTPStatus.OK)
        albums = response.json()["albums"]
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["name"], self.album.name)
        self.assertEqual(albums[0]["slug"], self.album.slug)
        self.assertIn("/add/", albums[0]["add_url"])
