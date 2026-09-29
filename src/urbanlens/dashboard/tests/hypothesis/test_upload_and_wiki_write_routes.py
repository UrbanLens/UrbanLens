"""Routes that accept files or start imports, attach or delete check-in media, and change a wiki's photos."""

from __future__ import annotations

import datetime
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.images import png_upload
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.images.issues import PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.immich.model import ImmichAccount
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


class _Users(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.stranger_user = baker.make(User)
        self.stranger = self.stranger_user.profile

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


class ImportStartRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("tools.import.start")

    def _zip(self, name: str = "export.zip") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, b"PK\x05\x06" + b"\x00" * 18, content_type="application/zip")

    def test_a_zip_starts_an_import_for_the_uploader_only(self) -> None:
        self.client.force_login(self.user)

        with mock.patch(_ENQUEUE) as enqueue:
            response = self.client.post(self.url, {"import_file": self._zip()})

        self.assertEqual(response.status_code, 200)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[1], self.user.pk)

    def test_a_missing_or_non_zip_file_is_400_and_starts_nothing(self) -> None:
        self.client.force_login(self.user)

        with mock.patch(_ENQUEUE) as enqueue:
            missing = self.client.post(self.url, {})
            wrong_type = self.client.post(self.url, {"import_file": png_upload("export.png")})

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(wrong_type.status_code, 400)
        enqueue.assert_not_called()

    def test_an_unavailable_queue_is_503_not_a_silent_success(self) -> None:
        self.client.force_login(self.user)

        with mock.patch(_ENQUEUE, return_value=None):
            response = self.client.post(self.url, {"import_file": self._zip()})

        self.assertEqual(response.status_code, 503)

    def test_anonymous_is_redirected_to_login(self) -> None:
        with mock.patch(_ENQUEUE) as enqueue:
            self.assert_login_redirect(self.client.post(self.url, {"import_file": self._zip()}))
        enqueue.assert_not_called()


class PinImmichImportRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.profile)
        ImmichAccount.objects.create(profile=self.profile, server_url="https://photos.example.com", api_key="k")
        self.url = reverse("pin.immich.import", args=[self.pin.slug])
        self.enqueue = mock.patch("urbanlens.dashboard.controllers.immich.safely_enqueue_task").start()
        self.addCleanup(mock.patch.stopall)

    def test_owner_enqueues_the_selected_assets_for_their_pin(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {"asset_ids": ["a1", "a2"]})

        self.assertEqual(response.status_code, 200)
        self.enqueue.assert_called_once()
        self.assertEqual(self.enqueue.call_args.args[1:4], (self.pin.pk, self.profile.pk, ["a1", "a2"]))

    def test_a_stranger_cannot_import_into_someone_elses_pin(self) -> None:
        ImmichAccount.objects.create(profile=self.stranger, server_url="https://evil.example.com", api_key="k")
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url, {"asset_ids": ["a1"]})

        self.assertEqual(response.status_code, 404)
        self.enqueue.assert_not_called()

    def test_no_assets_or_no_connection_is_400(self) -> None:
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.url, {}).status_code, 400)

        ImmichAccount.objects.filter(profile=self.profile).delete()
        self.assertEqual(self.client.post(self.url, {"asset_ids": ["a1"]}).status_code, 400)
        self.enqueue.assert_not_called()

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"asset_ids": ["a1"]}))
        self.enqueue.assert_not_called()


class PhotoUploadFailureRouteTests(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("vault.photos.failures")

    def test_records_a_failure_against_the_caller_and_their_own_pin(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        self.client.force_login(self.user)

        response = self.client.post(
            self.url,
            {"filename": "IMG_1.HEIC", "error": "decode", "pin_slug": pin.slug},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        failure = PhotoUploadFailure.objects.get()
        self.assertEqual((failure.profile_id, failure.pin_id), (self.profile.pk, pin.pk))

    def test_another_users_pin_is_not_linked(self) -> None:
        their_pin = baker.make(Pin, profile=self.stranger)
        self.client.force_login(self.user)

        self.client.post(self.url, {"filename": "x.jpg", "pin_slug": their_pin.slug}, content_type="application/json")

        self.assertIsNone(PhotoUploadFailure.objects.get().pin_id)

    def test_invalid_json_is_400(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, data="{nope", content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PhotoUploadFailure.objects.exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"filename": "x"}, content_type="application/json"))
        self.assertFalse(PhotoUploadFailure.objects.exists())

    @pytest.mark.xfail(
        strict=True,
        raises=AttributeError,
        reason="P29 bug: PhotoUploadFailureCreateView.post calls .get() on whatever JSON decodes, so a JSON array "
        "body raises (500)",
    )
    def test_a_json_array_body_is_a_4xx(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, data="[]", content_type="application/json")

        self.assertIn(response.status_code, range(400, 500))


class _ExternalSafetyFixture(_Users):
    def setUp(self) -> None:
        super().setUp()
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.profile,
            title="Hike",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        self.foreign_checkin = baker.make(
            SafetyCheckin,
            profile=self.stranger,
            title="Theirs",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        _key, raw = generate_api_key(self.user, "safety client")
        ApiKey.objects.filter(user=self.user).update(
            scopes=[ApiKeyScope.SAFETY_READ.value, ApiKeyScope.SAFETY_WRITE.value]
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


class ExternalSafetyPhotoAttachRouteTests(_ExternalSafetyFixture):
    def _attach(self, checkin: SafetyCheckin, body, **headers):
        url = reverse("external_api:safety.checkins.photos", args=[checkin.slug])
        return self.client.post(url, body, content_type="application/json", **headers)

    def test_attaches_the_callers_own_photo(self) -> None:
        image = baker.make(Image, profile=self.profile)

        response = self._attach(self.checkin, {"image_uuid": str(image.uuid)}, **self.auth)

        self.assertEqual(response.status_code, 201, response.content)
        image.refresh_from_db()
        self.assertEqual(image.safety_checkin_id, self.checkin.pk)

    def test_another_users_photo_cannot_be_attached(self) -> None:
        their_image = baker.make(Image, profile=self.stranger)

        response = self._attach(self.checkin, {"image_uuid": str(their_image.uuid)}, **self.auth)

        self.assertEqual(response.status_code, 404)
        their_image.refresh_from_db()
        self.assertIsNone(their_image.safety_checkin_id)

    def test_cannot_attach_to_another_users_checkin(self) -> None:
        image = baker.make(Image, profile=self.profile)

        response = self._attach(self.foreign_checkin, {"image_uuid": str(image.uuid)}, **self.auth)

        self.assertEqual(response.status_code, 404)
        image.refresh_from_db()
        self.assertIsNone(image.safety_checkin_id)

    def test_a_photo_on_another_checkin_is_not_moved(self) -> None:
        other_own_checkin = baker.make(
            SafetyCheckin,
            profile=self.profile,
            title="Earlier",
            checkin_by=timezone.now() + datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
        )
        image = baker.make(Image, profile=self.profile, safety_checkin=other_own_checkin)

        response = self._attach(self.checkin, {"image_uuid": str(image.uuid)}, **self.auth)

        self.assertEqual(response.status_code, 400)
        image.refresh_from_db()
        self.assertEqual(image.safety_checkin_id, other_own_checkin.pk)

    def test_anonymous_is_refused(self) -> None:
        image = baker.make(Image, profile=self.profile)

        response = self._attach(self.checkin, {"image_uuid": str(image.uuid)})

        self.assertIn(response.status_code, (401, 403))
        image.refresh_from_db()
        self.assertIsNone(image.safety_checkin_id)

    def test_malformed_bodies_are_400(self) -> None:
        for body in ({}, {"image_uuid": "nope"}, ["x"]):
            self.assertEqual(self._attach(self.checkin, body, **self.auth).status_code, 400, body)


class ExternalSafetyPhotoDeleteRouteTests(_ExternalSafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.image = baker.make(Image, profile=self.profile, safety_checkin=self.checkin)
        self.url = reverse("external_api:safety.checkins.photos.detail", args=[self.checkin.slug, self.image.pk])

    def test_owner_deletes_the_photo(self) -> None:
        response = self.client.delete(self.url, **self.auth)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())

    def test_a_photo_on_another_users_checkin_is_404(self) -> None:
        their_image = baker.make(Image, profile=self.stranger, safety_checkin=self.foreign_checkin)

        through_theirs = self.client.delete(
            reverse("external_api:safety.checkins.photos.detail", args=[self.foreign_checkin.slug, their_image.pk]),
            **self.auth,
        )
        through_mine = self.client.delete(
            reverse("external_api:safety.checkins.photos.detail", args=[self.checkin.slug, their_image.pk]), **self.auth
        )

        self.assertEqual((through_theirs.status_code, through_mine.status_code), (404, 404))
        self.assertTrue(Image.objects.filter(pk=their_image.pk).exists())

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self.client.delete(self.url).status_code, (401, 403))
        self.assertTrue(Image.objects.filter(pk=self.image.pk).exists())


class ExternalSafetyMapDetachRouteTests(_ExternalSafetyFixture):
    def setUp(self) -> None:
        super().setUp()
        self.map = baker.make(MarkupMap, profile=self.profile)
        self.checkin.markup_maps.add(self.map)

    def _delete(self, checkin: SafetyCheckin, map_uuid, **headers):
        return self.client.delete(
            reverse("external_api:safety.checkins.maps.detail", args=[checkin.slug, map_uuid]), **headers
        )

    def test_owner_detaches_and_the_map_survives(self) -> None:
        response = self._delete(self.checkin, self.map.uuid, **self.auth)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(self.checkin.markup_maps.exists())
        self.assertTrue(MarkupMap.objects.filter(pk=self.map.pk).exists())

    def test_another_users_checkin_is_404_and_its_map_stays(self) -> None:
        their_map = baker.make(MarkupMap, profile=self.stranger)
        self.foreign_checkin.markup_maps.add(their_map)

        response = self._delete(self.foreign_checkin, their_map.uuid, **self.auth)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(self.foreign_checkin.markup_maps.filter(pk=their_map.pk).exists())

    def test_a_map_not_on_this_checkin_is_404(self) -> None:
        response = self._delete(self.checkin, baker.make(MarkupMap, profile=self.profile).uuid, **self.auth)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(self.checkin.markup_maps.filter(pk=self.map.pk).exists())

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self._delete(self.checkin, self.map.uuid).status_code, (401, 403))
        self.assertTrue(self.checkin.markup_maps.filter(pk=self.map.pk).exists())


class _WikiFixture(_Users):
    """A wiki the user can see (they have a pin there) and the stranger cannot."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, official_name="Old Mill")
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)
        self.location.ensure_slug()


class WikiArticleImageUploadRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("location.wiki.article.image", args=[self.location.slug])

    def test_a_viewer_uploads_an_image_owned_by_them_on_the_wiki(self) -> None:
        self.client.force_login(self.user)

        with mock.patch(_ENQUEUE):
            response = self.client.post(self.url, {"image": png_upload()})

        self.assertEqual(response.status_code, 201, response.content)
        image = Image.objects.get(pk=response.json()["id"])
        self.assertEqual((image.profile_id, image.wiki_id), (self.profile.pk, self.wiki.pk))

    def test_someone_who_cannot_see_the_wiki_gets_404_and_nothing_is_stored(self) -> None:
        self.client.force_login(self.stranger_user)

        with mock.patch(_ENQUEUE):
            response = self.client.post(self.url, {"image": png_upload()})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Image.objects.filter(wiki=self.wiki).exists())

    def test_a_missing_or_non_image_file_is_a_4xx_and_nothing_is_stored(self) -> None:
        self.client.force_login(self.user)

        with mock.patch(_ENQUEUE):
            missing = self.client.post(self.url, {})
            not_image = self.client.post(
                self.url, {"image": SimpleUploadedFile("x.png", b"#!/bin/sh\necho hi\n", content_type="image/png")}
            )

        self.assertEqual(missing.status_code, 400)
        self.assertIn(not_image.status_code, range(400, 500))
        self.assertFalse(Image.objects.filter(wiki=self.wiki).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"image": png_upload()}))
        self.assertFalse(Image.objects.filter(wiki=self.wiki).exists())


class WikiAlbumUploadRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.album = baker.make(Album, parent_wiki=self.wiki, profile=self.profile, name="Exteriors")
        self.url = reverse("location.wiki.albums.upload", args=[self.location.slug, self.album.slug])

    def test_a_viewer_uploads_into_the_album(self) -> None:
        self.client.force_login(self.user)

        with mock.patch(_ENQUEUE):
            response = self.client.post(self.url, {"image": png_upload()})

        self.assertEqual(response.status_code, 201, response.content)
        item = AlbumItem.objects.get(album=self.album)
        self.assertEqual((item.image.profile_id, item.image.wiki_id), (self.profile.pk, self.wiki.pk))

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        with mock.patch(_ENQUEUE):
            response = self.client.post(self.url, {"image": png_upload()})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(AlbumItem.objects.filter(album=self.album).exists())

    def test_no_file_is_400(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AlbumItem.objects.filter(album=self.album).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"image": png_upload()}))
        self.assertFalse(AlbumItem.objects.filter(album=self.album).exists())


class WikiAlbumDeleteRouteTests(_WikiFixture):
    """Only the visibility gate is asserted: which viewers may delete a community album is not settled here."""

    def setUp(self) -> None:
        super().setUp()
        self.album = baker.make(Album, parent_wiki=self.wiki, profile=self.profile, name="Exteriors")
        self.url = reverse("location.wiki.albums.delete", args=[self.location.slug, self.album.slug])

    def test_its_creator_deletes_it(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Album.objects.filter(pk=self.album.pk).exists())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Album.objects.filter(pk=self.album.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertTrue(Album.objects.filter(pk=self.album.pk).exists())


class WikiOverlayDeleteRouteTests(_WikiFixture):
    """Only the visibility gate is asserted, as for community albums."""

    def setUp(self) -> None:
        super().setUp()
        corners = {f"{d}_{axis}": 0.0 for d in ("nw", "ne", "se", "sw") for axis in ("latitude", "longitude")}
        self.overlay = baker.make(
            MapImageOverlay,
            parent_wiki=self.wiki,
            profile=self.profile,
            tile_url_template="/map/historical-tiles/x/{z}/{x}/{y}.png",
            **corners,
        )
        self.url = reverse("location.wiki.overlays.delete", args=[self.location.slug, self.overlay.uuid])

    def test_its_creator_deletes_it(self) -> None:
        self.client.force_login(self.user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.filter(pk=self.overlay.pk).exists())

    def test_someone_who_cannot_see_the_wiki_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(MapImageOverlay.objects.filter(pk=self.overlay.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.delete(self.url))
        self.assertTrue(MapImageOverlay.objects.filter(pk=self.overlay.pk).exists())


class ExternalWikiCoverPhotoRouteTests(_WikiFixture):
    def setUp(self) -> None:
        super().setUp()
        _key, raw = generate_api_key(self.user, "wiki client")
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value])
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        _key, stranger_raw = generate_api_key(self.stranger_user, "stranger client")
        ApiKey.objects.filter(user=self.stranger_user).update(
            scopes=[ApiKeyScope.WIKI_READ.value, ApiKeyScope.WIKI_WRITE.value]
        )
        self.stranger_auth = {"HTTP_AUTHORIZATION": f"Bearer {stranger_raw}"}
        self.photo = baker.make(Image, profile=self.profile, wiki=self.wiki, location=self.location)
        self.url = reverse("external_api:wikis.cover_photo", args=[self.location.slug])

    def _cover_id(self) -> int | None:
        self.wiki.refresh_from_db()
        return self.wiki.cover_photo_id

    def _put(self, body, **headers):
        return self.client.put(self.url, body, content_type="application/json", **headers)

    def test_a_viewer_sets_then_clears_the_cover(self) -> None:
        self.assertEqual(self._put({"image_uuid": str(self.photo.uuid)}, **self.auth).status_code, 200)
        self.assertEqual(self._cover_id(), self.photo.pk)

        self.assertEqual(self.client.delete(self.url, **self.auth).status_code, 200)
        self.assertIsNone(self._cover_id())

    def test_a_photo_from_another_place_cannot_become_the_cover(self) -> None:
        elsewhere = baker.make(Location)
        foreign = baker.make(Image, profile=self.profile, wiki=baker.make(Wiki, location=elsewhere), location=elsewhere)

        response = self._put({"image_uuid": str(foreign.uuid)}, **self.auth)

        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self._cover_id())

    def test_someone_who_cannot_see_the_wiki_gets_404_for_both_verbs(self) -> None:
        Wiki.objects.filter(pk=self.wiki.pk).update(cover_photo=self.photo)

        put = self._put({"image_uuid": str(self.photo.uuid)}, **self.stranger_auth)
        delete = self.client.delete(self.url, **self.stranger_auth)

        self.assertEqual((put.status_code, delete.status_code), (404, 404))
        self.assertEqual(self._cover_id(), self.photo.pk)

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self._put({"image_uuid": str(self.photo.uuid)}).status_code, (401, 403))
        self.assertIsNone(self._cover_id())

    def test_a_malformed_body_is_400(self) -> None:
        for body in ({}, {"image_uuid": "nope"}, ["x"]):
            self.assertEqual(self._put(body, **self.auth).status_code, 400, body)
        self.assertIsNone(self._cover_id())
