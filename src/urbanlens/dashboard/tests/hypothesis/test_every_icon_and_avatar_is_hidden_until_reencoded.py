"""Every custom icon and avatar is re-encoded in the sandbox, and nobody else is served the upload before that (P119).

Pin and achievement icons were stored as uploaded and never re-encoded. Label icons and avatars were re-encoded, but
the field named the upload until the sandbox worker had run, and the media gate serves any icon or avatar path to any
signed-in member. Each writer below runs with its queued tasks held back; before they run, no stored file carrying the
fixture's marker may be authorized for another member, and after they run the field names a clean file.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import count
from pathlib import Path
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models.fields.files import FieldFile
from django.test import RequestFactory, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.access import authorize_media
from urbanlens.dashboard.tasks import SANDBOX_QUEUE
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import (
    MARK,
    SANDBOX,
    _everything_readable,
    _fixtures,
    _MetadataCase,
)
from urbanlens.dashboard.tests.hypothesis.test_every_stored_user_image_is_reencoded import _upload

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_SCAN_TARGET = "urbanlens.dashboard.services.security.malware_scan.malware_error_for_upload"
_names = count()

#: A writer stores an upload for the profile, and returns how to read back the field it wrote.
Writer = Callable[[Profile], Callable[[], FieldFile]]


class EveryIconAndAvatarTests(_MetadataCase):
    def setUp(self) -> None:
        super().setUp()
        self.viewer = self._profile()
        self.admin = User.objects.create(username=f"iconadmin{next(_names)}", is_superuser=True, is_staff=True)
        self.name, self.data = _fixtures()["png-text"]

    def _profile(self) -> Profile:
        return User.objects.create(username=f"iconowner{next(_names)}").profile

    def _served_carrying_the_marker(self) -> list[str]:
        root = Path(settings.MEDIA_ROOT)
        return [
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and MARK in _everything_readable(path.read_bytes())
            and authorize_media(self.viewer, path.relative_to(root).as_posix())
        ]

    def _run_queued(self, enqueue: mock.MagicMock) -> None:
        with override_settings(**SANDBOX):
            for queued in enqueue.call_args_list:
                task, *args = queued.args
                if getattr(task, "queue", None) == SANDBOX_QUEUE:
                    task(*args)

    def _label_create(self, profile: Profile) -> Callable[[], FieldFile]:
        name = f"ZzIcon {next(_names)}"
        self.client.force_login(profile.user)
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "tag"}),
            {"name": name, "custom_icon": _upload(self.name, self.data)},
        )
        self.assertLess(response.status_code, 400, response.content)
        return lambda: Label.objects.get(profile=profile, name=name).custom_icon

    def _label_edit(self, profile: Profile) -> Callable[[], FieldFile]:
        label = Label.objects.create(profile=profile, kind=KIND_TAG, name=f"ZzIcon {next(_names)}")
        self.client.force_login(profile.user)
        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.pk}),
            {"name": label.name, f"custom_icon-{label.pk}": _upload(self.name, self.data)},
        )
        self.assertLess(response.status_code, 400, response.content)
        return lambda: Label.objects.get(pk=label.pk).custom_icon

    def _pin_create(self, profile: Profile) -> Callable[[], FieldFile]:
        from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

        latitude = 40 + next(_names) / 100
        result = create_pin_for_profile(
            profile, name="Mill", latitude=latitude, longitude=-75.0, custom_icon=_upload(self.name, self.data)
        )
        return lambda: Pin.objects.get(pk=result.pin.pk).custom_icon

    def _pin_quick_edit(self, profile: Profile) -> Callable[[], FieldFile]:
        pin = baker.make(Pin, profile=profile, location=baker.make(Location), parent_pin=None)
        self.client.force_login(profile.user)
        response = self.client.post(
            f"/dashboard/map/quick-edit/{pin.slug or pin.uuid}/",
            {"name": "Mill", "custom_icon": _upload(self.name, self.data)},
        )
        self.assertLess(response.status_code, 400, response.content)
        return lambda: Pin.objects.get(pk=pin.pk).custom_icon

    def _achievement_form(self, name: str) -> dict[str, object]:
        return {
            "name": name,
            "metric": "photos_uploaded",
            "threshold": "10",
            "custom_icon": _upload(self.name, self.data),
        }

    def _achievement_create(self, profile: Profile) -> Callable[[], FieldFile]:
        name = f"Shutterbug {next(_names)}"
        self.client.force_login(self.admin)
        response = self.client.post(reverse("site_admin_achievements"), self._achievement_form(name))
        self.assertLess(response.status_code, 400, response.content)
        return lambda: Achievement.objects.get(name=name).custom_icon

    def _achievement_edit(self, profile: Profile) -> Callable[[], FieldFile]:
        achievement = Achievement.objects.create(
            name=f"Shutterbug {next(_names)}", metric="photos_uploaded", threshold=10
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("site_admin_achievement_edit", kwargs={"achievement_id": achievement.pk}),
            self._achievement_form(achievement.name),
        )
        self.assertLess(response.status_code, 400, response.content)
        return lambda: Achievement.objects.get(pk=achievement.pk).custom_icon

    def _avatar_upload(self, profile: Profile) -> Callable[[], FieldFile]:
        from urbanlens.dashboard.services.profile.avatar import set_profile_avatar

        set_profile_avatar(profile, _upload(self.name, self.data))
        return lambda: Profile.objects.get(pk=profile.pk).avatar

    def _avatar_social_login(self, profile: Profile) -> Callable[[], FieldFile]:
        from urbanlens.dashboard.services.profile.avatar import AvatarService
        from urbanlens.dashboard.services.social_auth.pipeline import fetch_and_save_avatar

        backend = mock.Mock()
        backend.name = "google-oauth2"
        with (
            mock.patch.object(AvatarService, "resolve_provider_url", return_value="https://example.test/a"),
            mock.patch.object(AvatarService, "download", return_value=self.data),
        ):
            fetch_and_save_avatar(backend, profile.user, {}, is_new=True)
        return lambda: Profile.objects.get(pk=profile.pk).avatar

    def _avatar_gravatar(self, profile: Profile) -> Callable[[], FieldFile]:
        from urbanlens.dashboard.controllers.userprofile import ProfileFieldUpdateView
        from urbanlens.dashboard.services.profile.avatar import AvatarService

        User.objects.filter(pk=profile.user.pk).update(email="someone@example.test")
        request = RequestFactory().post("/")
        request.user = User.objects.get(pk=profile.user.pk)
        with mock.patch.object(AvatarService, "download", return_value=self.data):
            ProfileFieldUpdateView()._save_avatar_gravatar(request, profile)
        return lambda: Profile.objects.get(pk=profile.pk).avatar

    def test_no_writer_serves_the_upload_and_every_one_ends_clean(self) -> None:
        writers: dict[str, Writer] = {
            "label create": self._label_create,
            "label edit": self._label_edit,
            "pin create": self._pin_create,
            "pin quick edit": self._pin_quick_edit,
            "achievement create": self._achievement_create,
            "achievement edit": self._achievement_edit,
            "avatar upload": self._avatar_upload,
            "avatar from social login": self._avatar_social_login,
            "avatar from Gravatar": self._avatar_gravatar,
        }
        for writer, write in writers.items():
            with self.subTest(writer):
                media_root = tempfile.mkdtemp(prefix="ul_icon_hidden_")
                self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
                with (
                    override_settings(MEDIA_ROOT=media_root),
                    mock.patch(_ENQUEUE) as enqueue,
                    mock.patch(_SCAN_TARGET, return_value=None),
                ):
                    with self.captureOnCommitCallbacks(execute=True):
                        stored = write(self._profile())
                    self.assertTrue(enqueue.call_args_list, "the writer queued nothing, so this proves nothing")
                    self.assertEqual(
                        self._served_carrying_the_marker(), [], "the upload is served before it is re-encoded"
                    )

                    self._run_queued(enqueue)

                    field = stored()
                    self.assertTrue(field, "nothing was kept, so this proves nothing")
                    self.assertClean(self._read(field), writer)
                    self.assertEqual(self._served_carrying_the_marker(), [])
