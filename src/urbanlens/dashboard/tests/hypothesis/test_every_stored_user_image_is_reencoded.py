"""Every stored user image, not only library photos, is re-encoded from its pixels (P119).

Comment and trip comment images, small label icons and avatars were stored as they came, so whatever metadata the
file carried was served with it. The fixtures and the detector are the photo matrix's: each fixture plants a marker in
one carrier, and the stored file must not carry it once the sandbox worker has run.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from itertools import count
import json
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.fields.files import FieldFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment
from urbanlens.dashboard.services.labels.icons import resize_stored_icon
from urbanlens.dashboard.services.profile.avatar import reencode_stored_avatar
from urbanlens.dashboard.tasks import scan_comment_image, scan_trip_comment_image
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import (
    _CONTENT_TYPES,
    SANDBOX,
    _fixtures,
    _MetadataCase,
)
from urbanlens.dashboard.tests.hypothesis.test_external_api_social_profile import (
    _bearer,
    _key_with_scopes,
    _multipart_put,
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

        def external_api(profile: Profile) -> None:
            key = _key_with_scopes(profile.user, ApiKeyScope.SOCIAL_WRITE, ApiKeyScope.PROFILE_READ)
            url = reverse("external_api:profiles.avatar", kwargs={"profile_slug": profile.slug or str(profile.uuid)})
            response = _multipart_put(self.client, url, {"file": _upload(name, data)}, _bearer(key))
            self.assertEqual(response.status_code, 200, response.content)

        writers = {
            "upload": lambda profile: set_profile_avatar(profile, _upload(name, data)),
            "social login": lambda profile: fetch_and_save_avatar(backend, profile.user, {}, is_new=True),
            "gravatar": gravatar,
            "external API": external_api,
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


class TheStoredNameTests(_Case):
    def test_no_reencoded_image_keeps_the_name_it_was_uploaded_with(self) -> None:
        """A file name can say as much as the metadata inside it, and these paths are served to other members."""
        _, data = _fixtures()["png-text"]
        telling = "42-elm-street.png"
        profile = self._profile()
        comment = Comment.objects.create(
            pin=baker.make(Pin, profile=profile),
            profile=profile,
            text="look",
            image=_upload(telling, data),
            pending_scan=True,
        )
        label = Label.objects.create(profile=profile, kind=KIND_TAG, name="ZzNamed", custom_icon=_upload(telling, data))
        profile.avatar = _upload(telling, data)
        profile.save(update_fields=["avatar"])

        with override_settings(**SANDBOX), mock.patch(_SCAN_TARGET, return_value=None):
            self.assertTrue(scan_comment_image(comment.pk))
            self.assertTrue(resize_stored_icon(label.pk, _stored_name(label.custom_icon)))
            self.assertTrue(reencode_stored_avatar(profile.pk, _stored_name(profile.avatar)))

        comment.refresh_from_db()
        label.refresh_from_db()
        profile.refresh_from_db()
        for stored in (comment.image, label.custom_icon, profile.avatar):
            with self.subTest(stored.field.name):
                self.assertNotIn("elm-street", _stored_name(stored))


class AFileThatCannotBeReadRightNowTests(_Case):
    """Storage failing to hand back a file says nothing about the file, so nothing is removed or rejected for it."""

    def _unreadable(self) -> AbstractContextManager[object]:
        return mock.patch.object(FieldFile, "open", side_effect=OSError("storage unavailable"))

    def _pending_comment(self) -> Comment:
        profile = self._profile()
        return Comment.objects.create(
            pin=baker.make(Pin, profile=profile),
            profile=profile,
            text="look",
            image=_upload(*_fixtures()["png-text"]),
            pending_scan=True,
        )

    def test_a_pending_comment_is_kept_for_a_retry(self) -> None:
        comment = self._pending_comment()

        with (
            override_settings(**SANDBOX),
            mock.patch(_SCAN_TARGET, return_value=None),
            self._unreadable(),
            self.assertRaises(OSError),
        ):
            scan_comment_image(comment.pk)

        comment.refresh_from_db()
        self.assertTrue(comment.pending_scan)
        self.assertTrue(comment.image)

    def test_a_pending_comment_still_unreadable_after_every_retry_is_never_published(self) -> None:
        comment = self._pending_comment()

        with (
            override_settings(**SANDBOX),
            mock.patch(_SCAN_TARGET, return_value=None),
            self._unreadable(),
            mock.patch.object(scan_comment_image, "max_retries", 0),
        ):
            self.assertFalse(scan_comment_image(comment.pk))

        self.assertFalse(Comment.objects.filter(pk=comment.pk, pending_scan=False).exists())

    def test_a_label_icon_is_kept(self) -> None:
        label = Label.objects.create(
            profile=self._profile(), kind=KIND_TAG, name="ZzUnreadable", custom_icon=_upload(*_fixtures()["png-text"])
        )
        name = _stored_name(label.custom_icon)

        with override_settings(**SANDBOX), self._unreadable(), self.assertRaises(OSError):
            resize_stored_icon(label.pk, name)

        label.refresh_from_db()
        self.assertEqual(label.custom_icon.name, name)
        self.assertTrue(label.custom_icon.storage.exists(name))

    def test_an_avatar_is_kept(self) -> None:
        profile = self._profile()
        profile.avatar = _upload(*_fixtures()["png-text"])
        profile.save(update_fields=["avatar"])
        name = _stored_name(profile.avatar)

        with override_settings(**SANDBOX), self._unreadable(), self.assertRaises(OSError):
            reencode_stored_avatar(profile.pk, name)

        profile.refresh_from_db()
        self.assertEqual(profile.avatar.name, name)
        self.assertTrue(profile.avatar.storage.exists(name))


class ALabelRestoredByUndoTests(_Case):
    def test_a_label_deleted_before_its_icon_was_reencoded_queues_the_reencode_when_restored(self) -> None:
        """The queued task finds no label and does nothing, so the restore is the only thing left to queue it."""
        from urbanlens.dashboard import tasks
        from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo

        profile = self._profile()
        label = Label.objects.create(
            profile=profile, kind=KIND_TAG, name="ZzUndo Icon", custom_icon=_upload(*_fixtures()["png-text"])
        )
        name = _stored_name(label.custom_icon)
        undo_action = stash_for_undo("label", [label], profile)
        assert undo_action is not None, "nothing was stashed"
        label.delete()

        with self.captureOnCommitCallbacks(execute=True), mock.patch(_ENQUEUE) as enqueue:
            (restored,) = restore_undo_action(undo_action)

        self.assertEqual(restored.custom_icon.name, name)
        self.assertIn(mock.call(tasks.resize_label_icon, restored.pk, name), enqueue.call_args_list)


class TheProfileFormTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.profile = self._profile()
        self.client.force_login(self.profile.user)

    def _save_form(self, **data: object) -> None:
        response = self.client.post(reverse("profile.edit"), data={"first_name": "Ada", **data})
        self.assertLess(response.status_code, 400)
        self.profile.user.refresh_from_db()
        self.assertEqual(self.profile.user.first_name, "Ada", "the form did not save, so this proves nothing")
        self.profile.refresh_from_db()

    def test_the_form_does_not_store_an_avatar(self) -> None:
        """An avatar goes through set_profile_avatar's checks and re-encode; the form decoded it in the request and
        stored it as sent."""
        with mock.patch(_ENQUEUE):
            self._save_form(avatar=_upload(*_fixtures()["jpeg-exif-gps"]))

        self.assertFalse(self.profile.avatar)

    def test_saving_the_form_keeps_an_avatar_reencoded_while_the_request_ran(self) -> None:
        """The form saved every column from the row it loaded, naming the file the re-encode had just deleted."""
        from urbanlens.dashboard.forms.profile_form import ProfileForm
        from urbanlens.dashboard.services.profile.avatar import set_profile_avatar

        with mock.patch(_ENQUEUE):
            set_profile_avatar(self.profile, _upload(*_fixtures()["png-text"]))
        uploaded = _stored_name(self.profile.avatar)
        validate = ProfileForm.is_valid

        def reencode_then_validate(form: ProfileForm) -> bool:
            with override_settings(**SANDBOX):
                self.assertTrue(reencode_stored_avatar(self.profile.pk, uploaded))
            return validate(form)

        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch.object(ProfileForm, "is_valid", autospec=True, side_effect=reencode_then_validate),
        ):
            self._save_form()

        self.assertNotEqual(self.profile.avatar.name, uploaded)
        self.assertTrue(self.profile.avatar.storage.exists(_stored_name(self.profile.avatar)))


class ALabelConvertedWhileItsIconIsReencodedTests(_Case):
    def test_the_convert_keeps_the_reencoded_icon(self) -> None:
        """The convert saved every column from the row it loaded, naming the file the re-encode had just deleted."""
        from urbanlens.dashboard.controllers import labels as label_views

        profile = self._profile()
        self.client.force_login(profile.user)
        label = Label.objects.create(
            profile=profile, kind=KIND_TAG, name="ZzConvert Icon", custom_icon=_upload(*_fixtures()["png-text"])
        )
        uploaded = _stored_name(label.custom_icon)
        apply_fields = label_views._apply_bulk_fields

        def reencode_then_apply(stale: Label, payload: dict) -> list[str]:
            with override_settings(**SANDBOX):
                self.assertTrue(resize_stored_icon(stale.pk, uploaded))
            return apply_fields(stale, payload)

        with mock.patch.object(label_views, "_apply_bulk_fields", side_effect=reencode_then_apply):
            response = self.client.post(
                reverse("label.bulk_convert", kwargs={"label_kind": "tags"}),
                data=json.dumps({"ids": [label.pk]}),
                content_type="application/json",
            )

        self.assertLess(response.status_code, 400)
        label.refresh_from_db()
        self.assertEqual(label.kind, KIND_CATEGORY, "the convert did not run, so this proves nothing")
        self.assertNotEqual(label.custom_icon.name, uploaded)
        self.assertTrue(label.custom_icon.storage.exists(_stored_name(label.custom_icon)))
