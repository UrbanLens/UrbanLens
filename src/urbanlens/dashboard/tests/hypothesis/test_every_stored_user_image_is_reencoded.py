"""Every stored user image, not only library photos, is re-encoded from its pixels (P119).

Comment and trip comment images, custom icons and avatars were stored as they came, so whatever metadata the file
carried was served with it. The fixtures and the detector are the photo matrix's: each fixture plants a marker in one
carrier, and the stored file must not carry it once the sandbox worker has run. Icons and avatars are held unserved
until then; ``test_held_icon_and_avatar_uploads`` covers what can happen while they wait.
"""

from __future__ import annotations

from itertools import count
import json
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.storage import Storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Model
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
from urbanlens.dashboard.services.media.held_upload import held_field, hold_upload
from urbanlens.dashboard.tasks import publish_held_upload, scan_comment_image, scan_trip_comment_image
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

    def _hold(self, instance: Model, field: str, name: str, data: bytes) -> str:
        instance.save(update_fields=[hold_upload(instance, field, _upload(name, data))])
        return getattr(instance, held_field(instance, field).upload_column)

    def _publish(self, instance: Model, field: str, held: str | None = None) -> bool:
        instance.refresh_from_db()
        spec = held_field(instance, field)
        with override_settings(**SANDBOX):
            published = publish_held_upload(spec.key, instance.pk, held or getattr(instance, spec.upload_column))
        instance.refresh_from_db()
        return published


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
        self._hold(label, "custom_icon", name, data)
        return label

    def test_an_icon_of_any_size_is_stored_without_metadata(self) -> None:
        for slug, (name, data) in _fixtures().items():
            with self.subTest(slug):
                label = self._label(name, data)

                self.assertTrue(self._publish(label, "custom_icon"))

                self.assertClean(self._read(label.custom_icon), "the label icon")

    def test_an_icon_that_cannot_be_reencoded_is_never_shown(self) -> None:
        label = self._label(*_UNDECODABLE)
        held = label.custom_icon_upload

        self.assertFalse(self._publish(label, "custom_icon"))

        self.assertFalse(label.custom_icon)
        self.assertEqual(label.custom_icon_upload, "")
        self.assertFalse(label.custom_icon.storage.exists(held))


class AnAvatarTests(_Case):
    def test_an_uploaded_avatar_is_stored_without_metadata(self) -> None:
        from urbanlens.dashboard.services.profile.avatar import set_profile_avatar

        for slug, (name, data) in _fixtures().items():
            with self.subTest(slug):
                profile = self._profile()
                with mock.patch(_ENQUEUE):
                    set_profile_avatar(profile, _upload(name, data))

                self.assertTrue(self._publish(profile, "avatar"))

                self.assertClean(self._read(profile.avatar), "the avatar")

    def test_every_way_an_avatar_is_saved_queues_its_publish(self) -> None:
        from django.test import RequestFactory

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
                self.assertTrue(profile.avatar_upload, "the writer held nothing, so this proves nothing")
                self.assertIn(
                    mock.call(publish_held_upload, "dashboard.Profile.avatar", profile.pk, profile.avatar_upload),
                    enqueue.call_args_list,
                )


class TheStoredNameTests(_Case):
    def test_no_image_keeps_the_name_it_was_uploaded_with(self) -> None:
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
        label = Label.objects.create(profile=profile, kind=KIND_TAG, name="ZzNamed")
        held = [self._hold(label, "custom_icon", telling, data), self._hold(profile, "avatar", telling, data)]
        self.assertEqual([name for name in held if "elm-street" in name], [])

        with override_settings(**SANDBOX), mock.patch(_SCAN_TARGET, return_value=None):
            self.assertTrue(scan_comment_image(comment.pk))
        self.assertTrue(self._publish(label, "custom_icon"))
        self.assertTrue(self._publish(profile, "avatar"))

        comment.refresh_from_db()
        for stored in (comment.image, label.custom_icon, profile.avatar):
            with self.subTest(stored.field.name):
                self.assertNotIn("elm-street", _stored_name(stored))


class AFileThatCannotBeReadRightNowTests(_Case):
    """Storage failing to hand back a file says nothing about the file, so nothing is removed or rejected for it."""

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
            mock.patch.object(FieldFile, "open", side_effect=OSError("storage unavailable")),
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
            mock.patch.object(FieldFile, "open", side_effect=OSError("storage unavailable")),
            mock.patch.object(scan_comment_image, "max_retries", 0),
        ):
            self.assertFalse(scan_comment_image(comment.pk))

        self.assertFalse(Comment.objects.filter(pk=comment.pk, pending_scan=False).exists())

    def test_a_held_icon_is_kept_for_a_retry(self) -> None:
        label = Label.objects.create(profile=self._profile(), kind=KIND_TAG, name="ZzUnreadable")
        held = self._hold(label, "custom_icon", *_fixtures()["png-text"])

        with mock.patch.object(Storage, "open", side_effect=OSError("storage unavailable")), self.assertRaises(OSError):
            self._publish(label, "custom_icon")

        label.refresh_from_db()
        self.assertEqual(label.custom_icon_upload, held)
        self.assertTrue(label.custom_icon.storage.exists(held))


class ALabelRestoredByUndoTests(_Case):
    def test_a_label_deleted_while_its_icon_waited_queues_the_publish_when_restored(self) -> None:
        """The queued task finds no label and publishes nothing, so the restore is the only thing left to queue it."""
        from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo

        profile = self._profile()
        label = Label.objects.create(profile=profile, kind=KIND_TAG, name="ZzUndo Icon")
        held = self._hold(label, "custom_icon", *_fixtures()["png-text"])
        undo_action = stash_for_undo("label", [label], profile)
        assert undo_action is not None, "nothing was stashed"
        label.delete()

        with self.captureOnCommitCallbacks(execute=True), mock.patch(_ENQUEUE) as enqueue:
            (restored,) = restore_undo_action(undo_action)

        self.assertEqual(restored.custom_icon_upload, held)
        self.assertIn(
            mock.call(publish_held_upload, "dashboard.Label.custom_icon", restored.pk, held), enqueue.call_args_list
        )


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
        self.assertEqual(self.profile.avatar_upload, "")

    def test_saving_the_form_keeps_an_avatar_published_while_the_request_ran(self) -> None:
        """A form that saved every column from the row it loaded would name no avatar and the upload already used."""
        from urbanlens.dashboard.forms.profile_form import ProfileForm
        from urbanlens.dashboard.services.profile.avatar import set_profile_avatar

        with mock.patch(_ENQUEUE):
            set_profile_avatar(self.profile, _upload(*_fixtures()["png-text"]))
        validate = ProfileForm.is_valid

        def publish_then_validate(form: ProfileForm) -> bool:
            self.assertTrue(self._publish(Profile.objects.get(pk=self.profile.pk), "avatar"))
            return validate(form)

        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch.object(ProfileForm, "is_valid", autospec=True, side_effect=publish_then_validate),
        ):
            self._save_form()

        self.assertEqual(self.profile.avatar_upload, "")
        self.assertTrue(self.profile.avatar.storage.exists(_stored_name(self.profile.avatar)))


class ALabelConvertedWhileItsIconIsPublishedTests(_Case):
    def test_the_convert_keeps_the_published_icon(self) -> None:
        """A convert that saved every column from the row it loaded would name no icon and the upload already used."""
        from urbanlens.dashboard.controllers import labels as label_views

        profile = self._profile()
        self.client.force_login(profile.user)
        label = Label.objects.create(profile=profile, kind=KIND_TAG, name="ZzConvert Icon")
        self._hold(label, "custom_icon", *_fixtures()["png-text"])
        apply_fields = label_views._apply_bulk_fields

        def publish_then_apply(stale: Label, payload: dict) -> list[str]:
            self.assertTrue(self._publish(Label.objects.get(pk=stale.pk), "custom_icon"))
            return apply_fields(stale, payload)

        with mock.patch.object(label_views, "_apply_bulk_fields", side_effect=publish_then_apply):
            response = self.client.post(
                reverse("label.bulk_convert", kwargs={"label_kind": "tags"}),
                data=json.dumps({"ids": [label.pk]}),
                content_type="application/json",
            )

        self.assertLess(response.status_code, 400)
        label.refresh_from_db()
        self.assertEqual(label.kind, KIND_CATEGORY, "the convert did not run, so this proves nothing")
        self.assertEqual(label.custom_icon_upload, "")
        self.assertTrue(label.custom_icon.storage.exists(_stored_name(label.custom_icon)))
