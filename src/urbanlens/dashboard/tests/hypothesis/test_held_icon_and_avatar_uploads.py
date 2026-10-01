"""A held icon or avatar is published only while its row still holds it, and a held file is never left behind (P119).

``test_every_icon_and_avatar_is_hidden_until_reencoded`` drives each writer; these drive what can happen between the
upload and the sandbox worker running.
"""

from __future__ import annotations

from itertools import count
import shutil
import tempfile
from unittest import mock

from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.db.models import Model
from django.test import RequestFactory, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.held_upload import discard_held_upload, held_field, hold_upload
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import SANDBOX, _fixtures, _MetadataCase
from urbanlens.dashboard.tests.hypothesis.test_external_api_social_profile import _bearer, _key_with_scopes

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_owners = count()


class _HeldCase(_MetadataCase):
    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_held_upload_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=media_root)
        media.enable()
        self.addCleanup(media.disable)

    def _profile(self) -> Profile:
        return User.objects.create(username=f"heldowner{next(_owners)}").profile

    def _pin(self) -> Pin:
        return baker.make(Pin, profile=self._profile(), location=baker.make(Location), parent_pin=None)

    def _hold(self, instance: Model, field: str, data: bytes | None = None) -> str:
        upload = ContentFile(_fixtures()["png-text"][1] if data is None else data)
        with self.captureOnCommitCallbacks(execute=True):
            instance.save(update_fields=[hold_upload(instance, field, upload)])
        name = getattr(instance, held_field(instance, field).upload_column)
        self.assertTrue(default_storage.exists(name))
        return name

    def _publish(self, instance: Model, field: str, name: str) -> bool:
        with override_settings(**SANDBOX):
            return tasks.publish_held_upload(held_field(instance, field).key, instance.pk, name)


class WhatHappensBeforeTheWorkerRunsTests(_HeldCase):
    def test_an_upload_replaced_before_it_was_published_is_never_published(self) -> None:
        profile = self._profile()
        first = self._hold(profile, "avatar")
        second = self._hold(profile, "avatar")

        self.assertFalse(default_storage.exists(first))
        self.assertFalse(self._publish(profile, "avatar", first))
        self.assertTrue(self._publish(profile, "avatar", second))

        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, "")
        self.assertClean(self._read(profile.avatar), "the published avatar")
        self.assertFalse(default_storage.exists(second))

    def test_an_icon_cleared_while_its_upload_waited_stays_cleared(self) -> None:
        pin = self._pin()
        held = self._hold(pin, "custom_icon")
        with self.captureOnCommitCallbacks(execute=True):
            pin.custom_icon = None
            pin.save(update_fields=["custom_icon", discard_held_upload(pin, "custom_icon")])

        self.assertFalse(self._publish(pin, "custom_icon", held))
        pin.refresh_from_db()
        self.assertFalse(pin.custom_icon)
        self.assertFalse(default_storage.exists(held))

    def test_a_row_deleted_while_its_upload_waited_publishes_nothing(self) -> None:
        label = Label.objects.create(profile=self._profile(), kind=KIND_TAG, name="ZzHeld Gone")
        held = self._hold(label, "custom_icon")
        label_id = label.pk
        label.delete()
        label.pk = label_id

        self.assertFalse(self._publish(label, "custom_icon", held))
        self.assertFalse(default_storage.exists("label_icons") and default_storage.listdir("label_icons")[1])


class WhatThePublishKeepsAndDeletesTests(_HeldCase):
    def test_an_undecodable_upload_is_dropped_and_the_field_keeps_what_it_showed(self) -> None:
        profile = self._profile()
        self.assertTrue(self._publish(profile, "avatar", self._hold(profile, "avatar")))
        profile.refresh_from_db()
        shown = profile.avatar.name

        held = self._hold(profile, "avatar", b"not an image at all")
        self.assertFalse(self._publish(profile, "avatar", held))

        profile.refresh_from_db()
        self.assertEqual((profile.avatar.name, profile.avatar_upload), (shown, ""))
        self.assertFalse(default_storage.exists(held))

    def test_a_replaced_avatar_is_deleted_and_a_replaced_label_icon_is_kept_for_undo(self) -> None:
        label = Label.objects.create(profile=self._profile(), kind=KIND_TAG, name="ZzHeld Kept")
        for instance, field, kept in ((self._profile(), "avatar", False), (label, "custom_icon", True)):
            with self.subTest(field):
                self.assertTrue(self._publish(instance, field, self._hold(instance, field)))
                instance.refresh_from_db()
                replaced = getattr(instance, field).name
                self.assertTrue(self._publish(instance, field, self._hold(instance, field)))
                self.assertEqual(default_storage.exists(replaced), kept)

    def test_a_storage_failure_is_retried_and_after_the_last_retry_the_upload_waits_for_storage(self) -> None:
        profile = self._profile()
        held = self._hold(profile, "avatar")
        key = held_field(profile, "avatar").key

        with (
            override_settings(**SANDBOX),
            mock.patch.object(Storage, "open", side_effect=OSError("storage unavailable")),
        ):
            with self.assertRaises(OSError):
                tasks.publish_held_upload(key, profile.pk, held)
            profile.refresh_from_db()
            self.assertEqual(profile.avatar_upload, held)

            with mock.patch.object(tasks.publish_held_upload, "max_retries", 0):
                self.assertFalse(tasks.publish_held_upload(key, profile.pk, held))

        profile.refresh_from_db()
        self.assertEqual(profile.avatar_upload, held)
        self.assertTrue(default_storage.exists(held))


class ARowReadBeforeThePublishTests(_HeldCase):
    """Settings forms, the external API and the detail pin dialog save every column of a row they read earlier."""

    def _rows(self) -> list[tuple[Model, str]]:
        return [
            (self._profile(), "avatar"),
            (Label.objects.create(profile=self._profile(), kind=KIND_TAG, name="ZzHeld Stale"), "custom_icon"),
            (self._pin(), "custom_icon"),
            (Achievement.objects.create(name="ZzHeld Stale", metric="photos_uploaded", threshold=10), "custom_icon"),
        ]

    def test_a_full_save_of_a_row_read_before_the_publish_keeps_the_published_file(self) -> None:
        for instance, field in self._rows():
            with self.subTest(type(instance).__name__):
                held = self._hold(instance, field)
                stale = type(instance)._default_manager.get(pk=instance.pk)
                self.assertTrue(self._publish(instance, field, held))
                instance.refresh_from_db()
                published = getattr(instance, field).name

                with self.captureOnCommitCallbacks(execute=True):
                    stale.save()

                instance.refresh_from_db()
                self.assertEqual((getattr(instance, field).name, getattr(instance, f"{field}_upload")), (published, ""))
                self.assertTrue(default_storage.exists(published))

    def test_a_full_save_still_writes_an_icon_it_changed(self) -> None:
        for instance, field in self._rows():
            with self.subTest(type(instance).__name__):
                self.assertTrue(self._publish(instance, field, self._hold(instance, field)))
                row = type(instance)._default_manager.get(pk=instance.pk)
                setattr(row, field, None)
                row.save()

                instance.refresh_from_db()
                self.assertFalse(getattr(instance, field))


class WhatElseHoldsAnUploadTests(_HeldCase):
    def test_a_pin_deleted_while_its_icon_waited_publishes_it_when_restored(self) -> None:
        from urbanlens.dashboard.services.undo.service import restore_undo_action, stash_for_undo

        pin = self._pin()
        held = self._hold(pin, "custom_icon")
        undo_action = stash_for_undo("pin", [pin], pin.profile)
        assert undo_action is not None, "nothing was stashed"
        pin.delete()

        with self.captureOnCommitCallbacks(execute=True), mock.patch(_ENQUEUE) as enqueue:
            (restored,) = restore_undo_action(undo_action)

        self.assertIn(
            mock.call(tasks.publish_held_upload, "dashboard.Pin.custom_icon", restored.pk, held), enqueue.call_args_list
        )

    def test_an_emoji_avatar_or_a_removed_one_is_not_overwritten_by_an_upload_still_waiting(self) -> None:
        from urbanlens.dashboard.services.profile.avatar import clear_profile_avatar, set_profile_avatar_from_emoji

        for choose in (
            lambda profile: set_profile_avatar_from_emoji(profile, "fox", "#e53935"),
            clear_profile_avatar,
        ):
            with self.subTest(choose):
                profile = self._profile()
                held = self._hold(profile, "avatar")
                with self.captureOnCommitCallbacks(execute=True):
                    choose(profile)
                profile.refresh_from_db()
                chosen = profile.avatar.name

                self.assertFalse(self._publish(profile, "avatar", held))
                profile.refresh_from_db()
                self.assertEqual(profile.avatar.name, chosen)
                self.assertFalse(default_storage.exists(held))

    def test_deleting_an_account_deletes_its_held_uploads(self) -> None:
        from urbanlens.dashboard.services.profile.account_deletion import hard_delete_profile

        profile = self._profile()
        pin = baker.make(Pin, profile=profile, location=baker.make(Location), parent_pin=None)
        label = Label.objects.create(profile=profile, kind=KIND_TAG, name="ZzHeld Account")
        held = [self._hold(profile, "avatar"), self._hold(pin, "custom_icon"), self._hold(label, "custom_icon")]

        with self.captureOnCommitCallbacks(execute=True):
            hard_delete_profile(profile)

        self.assertEqual([name for name in held if default_storage.exists(name)], [])

    def test_a_deleted_achievement_takes_its_held_icon_with_it(self) -> None:
        achievement = Achievement.objects.create(name="ZzHeld Award", metric="photos_uploaded", threshold=10)
        held = self._hold(achievement, "custom_icon")

        with self.captureOnCommitCallbacks(execute=True):
            achievement.delete()

        self.assertFalse(default_storage.exists(held))

    def test_the_owner_is_told_an_avatar_is_processing_and_nobody_else_is(self) -> None:
        profile = self._profile()
        self._hold(profile, "avatar")
        viewer = self._profile()
        for caller, expected in ((profile, True), (viewer, False)):
            with self.subTest(owner=expected), mock.patch.object(Profile, "can_view_profile", return_value=True):
                key = _key_with_scopes(caller.user, ApiKeyScope.PROFILE_READ, ApiKeyScope.SOCIAL_READ)
                response = self.client.get(
                    reverse("external_api:profiles.detail", kwargs={"profile_slug": profile.uuid}), **_bearer(key)
                )
                self.assertEqual(response.status_code, 200, response.content)
                self.assertIs(response.json()["avatar_pending"], expected)

    def test_the_django_admin_cannot_store_an_achievement_icon(self) -> None:
        request = RequestFactory().get("/")
        request.user = User.objects.create(username=f"heldadmin{next(_owners)}", is_superuser=True, is_staff=True)
        form = site._registry[Achievement].get_form(
            request, Achievement.objects.create(name="ZzHeld Admin", metric="photos_uploaded", threshold=1)
        )

        self.assertNotIn("custom_icon", form.base_fields)
