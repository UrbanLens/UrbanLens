"""Every stored user image, not only library photos, is re-encoded from its pixels (P119).

Comment and trip comment images, small label icons and avatars were stored as they came, so whatever metadata the
file carried was served with it. The fixtures and the detector are the photo matrix's: each fixture plants a marker in
one carrier, and the stored file must not carry it once the sandbox worker has run.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.fields.files import FieldFile
from django.test import override_settings
from model_bakery import baker

from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment
from urbanlens.dashboard.services.labels.icons import resize_stored_icon
from urbanlens.dashboard.tasks import scan_comment_image, scan_trip_comment_image
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import (
    _CONTENT_TYPES,
    SANDBOX,
    _fixtures,
    _MetadataCase,
)

_SCAN_TARGET = "urbanlens.dashboard.services.security.malware_scan.malware_error_for_upload"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_UNDECODABLE = ("broken.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
_owners = count()


def _upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data, content_type=_CONTENT_TYPES[Path(name).suffix])


def _stored_name(stored: FieldFile) -> str:
    assert stored.name, "nothing is stored"
    return stored.name


class _Case(_MetadataCase):
    def _profile(self) -> Profile:
        return User.objects.create(username=f"p119owner{next(_owners)}").profile


class ACommentImageTests(_Case):
    def test_a_comment_image_is_stored_without_metadata(self) -> None:
        for slug, (name, data) in _fixtures().items():
            with self.subTest(slug):
                profile = self._profile()
                comment = Comment.objects.create(
                    pin=baker.make(Pin, profile=profile),
                    profile=profile,
                    text="look",
                    image=_upload(name, data),
                    pending_scan=True,
                )

                with override_settings(**SANDBOX), mock.patch(_SCAN_TARGET, return_value=None):
                    self.assertTrue(scan_comment_image(comment.pk))

                comment.refresh_from_db()
                self.assertFalse(comment.pending_scan)
                self.assertClean(self._read(comment.image), "the comment image")

    def test_a_trip_comment_image_is_stored_without_metadata(self) -> None:
        for slug, (name, data) in _fixtures().items():
            with self.subTest(slug):
                profile = self._profile()
                comment = TripComment.objects.create(
                    trip=baker.make(Trip, creator=profile),
                    author=profile,
                    text="look",
                    image=_upload(name, data),
                    pending_scan=True,
                )

                with override_settings(**SANDBOX), mock.patch(_SCAN_TARGET, return_value=None):
                    self.assertTrue(scan_trip_comment_image(comment.pk))

                comment.refresh_from_db()
                self.assertFalse(comment.pending_scan)
                self.assertClean(self._read(comment.image), "the trip comment image")

    def test_a_comment_image_that_cannot_be_reencoded_is_never_published(self) -> None:
        profile = self._profile()
        comment = Comment.objects.create(
            pin=baker.make(Pin, profile=profile),
            profile=profile,
            text="look",
            image=_upload(*_UNDECODABLE),
            pending_scan=True,
        )

        with override_settings(**SANDBOX), mock.patch(_SCAN_TARGET, return_value=None):
            self.assertFalse(scan_comment_image(comment.pk))

        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())


class ALabelIconTests(_Case):
    def _label(self, name: str, data: bytes) -> Label:
        label = baker.make(Label, profile=self._profile(), kind=KIND_TAG, name="Urbex")
        label.custom_icon = _upload(name, data)
        label.save()
        return label

    def test_an_icon_of_any_size_is_stored_without_metadata(self) -> None:
        for slug, (name, data) in _fixtures().items():
            with self.subTest(slug):
                label = self._label(name, data)

                with override_settings(**SANDBOX):
                    self.assertTrue(resize_stored_icon(label.pk, _stored_name(label.custom_icon)))

                label.refresh_from_db()
                self.assertClean(self._read(label.custom_icon), "the label icon")

    def test_an_icon_that_cannot_be_reencoded_is_removed(self) -> None:
        label = self._label(*_UNDECODABLE)
        name, storage = _stored_name(label.custom_icon), label.custom_icon.storage

        with override_settings(**SANDBOX):
            resize_stored_icon(label.pk, name)

        label.refresh_from_db()
        self.assertFalse(label.custom_icon)
        self.assertFalse(storage.exists(name))


class AnAvatarTests(_Case):
    def _reencode(self, profile: Profile) -> None:
        from urbanlens.dashboard import tasks

        profile.refresh_from_db()
        with override_settings(**SANDBOX):
            tasks.reencode_profile_avatar(profile.pk, profile.avatar.name)
        profile.refresh_from_db()

    def test_an_uploaded_avatar_is_stored_without_metadata(self) -> None:
        from urbanlens.dashboard.services.profile.avatar import set_profile_avatar

        for slug, (name, data) in _fixtures().items():
            with self.subTest(slug):
                profile = self._profile()
                with mock.patch(_ENQUEUE):
                    set_profile_avatar(profile, _upload(name, data))

                self._reencode(profile)

                self.assertClean(self._read(profile.avatar), "the avatar")

    def test_an_avatar_that_cannot_be_reencoded_is_removed(self) -> None:
        profile = self._profile()
        profile.avatar = _upload(*_UNDECODABLE)
        profile.save(update_fields=["avatar"])
        name, storage = profile.avatar.name, profile.avatar.storage

        self._reencode(profile)

        self.assertFalse(profile.avatar)
        self.assertFalse(storage.exists(name))

    def test_an_avatar_replaced_since_the_reencode_was_queued_is_left_alone(self) -> None:
        from urbanlens.dashboard import tasks

        name, data = _fixtures()["png-text"]
        profile = self._profile()
        profile.avatar = _upload(name, data)
        profile.save(update_fields=["avatar"])
        stale = profile.avatar.name
        profile.avatar = _upload("newer.png", data)
        profile.save(update_fields=["avatar"])
        current = profile.avatar.name

        with override_settings(**SANDBOX):
            self.assertFalse(tasks.reencode_profile_avatar(profile.pk, stale))

        profile.refresh_from_db()
        self.assertEqual(profile.avatar.name, current)

    def test_every_way_an_avatar_is_saved_queues_its_reencode(self) -> None:
        from django.test import RequestFactory

        from urbanlens.dashboard import tasks
        from urbanlens.dashboard.controllers.userprofile import ProfileFieldUpdateView
        from urbanlens.dashboard.services.profile.avatar import AvatarService, set_profile_avatar
        from urbanlens.dashboard.services.social_auth.pipeline import fetch_and_save_avatar

        name, data = _fixtures()["jpeg-exif-gps"]
        backend = mock.Mock()
        backend.name = "google-oauth2"

        def gravatar(profile: Profile) -> None:
            User.objects.filter(pk=profile.user.pk).update(email="someone@example.test")
            request = RequestFactory().post("/")
            request.user = User.objects.get(pk=profile.user.pk)
            ProfileFieldUpdateView()._save_avatar_gravatar(request, profile)

        writers = {
            "upload": lambda profile: set_profile_avatar(profile, _upload(name, data)),
            "social login": lambda profile: fetch_and_save_avatar(backend, profile.user, {}, is_new=True),
            "gravatar": gravatar,
        }
        for writer, save in writers.items():
            with self.subTest(writer):
                profile = self._profile()
                with (
                    self.captureOnCommitCallbacks(execute=True),
                    mock.patch(_ENQUEUE) as enqueue,
                    mock.patch.object(AvatarService, "resolve_provider_url", return_value="https://example.test/a"),
                    mock.patch.object(AvatarService, "download", return_value=data),
                ):
                    save(profile)

                profile.refresh_from_db()
                self.assertTrue(profile.avatar, "the writer stored nothing, so this proves nothing")
                self.assertIn(
                    mock.call(tasks.reencode_profile_avatar, profile.pk, profile.avatar.name), enqueue.call_args_list
                )
