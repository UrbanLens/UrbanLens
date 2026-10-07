"""An icon upload is refused over its own ceiling before anything reads it, and no stored icon, avatar or comment
image is read into memory past the ceiling its door applied.

An icon is shown at most 256px, yet its only limit was the site-wide upload cap (250 MB by default), so the in-request
antivirus scan and the sandbox worker's re-encode both read whole files of that size.
"""

from __future__ import annotations

from itertools import count
import shutil
import tempfile
from typing import Self
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard import tasks
from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.media.held_upload import held_field, hold_upload
from urbanlens.dashboard.services.media.stored_field import Reencoded, reencode_stored_field
from urbanlens.dashboard.tests.hypothesis.test_every_stored_photo_is_reencoded import SANDBOX, _fixtures, _MetadataCase

_CEILING = 4096
_GATE = "urbanlens.dashboard.services.media.images.image_upload_error"
_names = count()


def _oversized_png() -> SimpleUploadedFile:
    name, data = _fixtures()["png-text"]
    return SimpleUploadedFile(name, data + b"\0" * (_CEILING + 1 - len(data)), content_type="image/png")


class _RefusesUnboundedReads:
    """A stored file whose reads must say how much they want, and want no more than one byte past the ceiling."""

    def __init__(self, handle, limit: int) -> None:  # noqa: ANN001 - whatever storage opened
        self._handle, self._limit = handle, limit

    def read(self, size: int = -1) -> bytes:
        assert 0 <= size <= self._limit + 1, f"read({size}) of a file whose reader allows {self._limit} bytes"  # nosec B101 - a test double
        return self._handle.read(size)

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._handle.close()


_REAL_OPEN = FileSystemStorage.open


def _bounded_open(storage: FileSystemStorage, name: str, mode: str = "rb") -> _RefusesUnboundedReads:
    return _RefusesUnboundedReads(_REAL_OPEN(storage, name, mode), _CEILING)


@override_settings(ICON_MAX_UPLOAD_BYTES=_CEILING)
class AnOversizedIconIsRefusedAtTheDoorTests(_MetadataCase):
    """Each door refuses it before the upload gauntlet, whose antivirus scan copies the whole file."""

    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_icon_ceiling_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media_root))
        self.gate = self.enterContext(mock.patch(_GATE, return_value=None))
        self.profile: Profile = User.objects.create(username=f"iconceiling{next(_names)}").profile
        self.client.force_login(self.profile.user)

    def _assert_refused(self, response, held: str) -> None:  # noqa: ANN001 - a test client response
        self.assertGreaterEqual(response.status_code, 400, response.content)
        self.assertIn(b"too large", response.content.lower())
        self.gate.assert_not_called()
        self.assertEqual(held, "", "the oversized icon was held for the worker anyway")

    def test_creating_a_label(self) -> None:
        name = f"ZzCeiling {next(_names)}"
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "tag"}), {"name": name, "custom_icon": _oversized_png()}
        )

        self._assert_refused(response, "")
        self.assertFalse(Label.objects.filter(profile=self.profile, name=name).exists())

    def test_editing_a_label(self) -> None:
        label = Label.objects.create(profile=self.profile, kind=KIND_TAG, name=f"ZzCeiling {next(_names)}")
        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tag", "label_id": label.pk}),
            {"name": label.name, f"custom_icon-{label.pk}": _oversized_png()},
        )

        label.refresh_from_db()
        self._assert_refused(response, label.custom_icon_upload)

    def test_adding_a_pin(self) -> None:
        response = self.client.post(
            reverse("pin.add"),
            {"name": "Mill", "latitude": "40.5", "longitude": "-75.5", "custom_icon": _oversized_png()},
        )

        self._assert_refused(response, "")
        self.assertFalse(Pin.objects.filter(profile=self.profile).exists())

    def test_editing_a_pin(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=baker.make(Location), parent_pin=None)
        response = self.client.post(
            reverse("pin.quick_edit", kwargs={"pin_slug": pin.slug or pin.uuid}),
            {"name": "Mill", "custom_icon": _oversized_png()},
        )

        pin.refresh_from_db()
        self._assert_refused(response, pin.custom_icon_upload)

    def test_an_award(self) -> None:
        admin = User.objects.create(username=f"iconceilingadmin{next(_names)}", is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        name = f"Shutterbug {next(_names)}"
        response = self.client.post(
            reverse("site_admin_achievements"),
            {"name": name, "metric": "photos_uploaded", "threshold": "10", "custom_icon": _oversized_png()},
        )

        self.gate.assert_not_called()
        self.assertFalse(Achievement.objects.filter(name=name).exists(), response.content)

    def test_an_icon_at_the_ceiling_still_reaches_the_gauntlet(self) -> None:
        name, data = _fixtures()["png-text"]
        self.assertLessEqual(len(data), _CEILING)
        self.client.post(
            reverse("label.create", kwargs={"label_kind": "tag"}),
            {
                "name": f"ZzCeiling {next(_names)}",
                "custom_icon": SimpleUploadedFile(name, data, content_type="image/png"),
            },
        )

        self.gate.assert_called_once()


class AHeldUploadIsReadOnlyUpToItsCeilingTests(_MetadataCase):
    """The door is not the only writer a future change could add, so the worker bounds its own read."""

    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_held_ceiling_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media_root))

    def _publish(self, label: Label, held: str):  # noqa: ANN202
        with (
            override_settings(**SANDBOX),
            mock.patch.object(FileSystemStorage, "open", _bounded_open),
            mock.patch("urbanlens.dashboard.services.media.images.reencode_image_file") as reencode,
        ):
            published = tasks.publish_held_upload(held_field(label, "custom_icon").key, label.pk, held)
        return published, reencode

    @override_settings(ICON_MAX_UPLOAD_BYTES=_CEILING)
    def test_an_icon_over_the_ceiling_is_dropped_unread(self) -> None:
        label = Label.objects.create(
            profile=User.objects.create(username=f"heldceiling{next(_names)}").profile,
            kind=KIND_TAG,
            name=f"ZzHeldCeiling {next(_names)}",
        )
        with self.captureOnCommitCallbacks(execute=False):
            label.save(update_fields=[hold_upload(label, "custom_icon", ContentFile(b"\0" * (_CEILING + 1)))])
        held = label.custom_icon_upload

        published, reencode = self._publish(label, held)

        self.assertFalse(published)
        reencode.assert_not_called()
        label.refresh_from_db()
        self.assertEqual(label.custom_icon_upload, "", "the oversized upload stayed held, to be tried again")
        self.assertFalse(default_storage.exists(held))


class AStoredImageIsReadOnlyUpToItsCeilingTests(_MetadataCase):
    def setUp(self) -> None:
        super().setUp()
        media_root = tempfile.mkdtemp(prefix="ul_stored_ceiling_")
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        self.enterContext(override_settings(MEDIA_ROOT=media_root))

    def test_an_image_over_the_ceiling_is_not_reencoded(self) -> None:
        label = Label.objects.create(
            profile=User.objects.create(username=f"storedceiling{next(_names)}").profile,
            kind=KIND_TAG,
            name=f"ZzStored {next(_names)}",
        )
        label.custom_icon.save("big.png", ContentFile(b"\0" * (_CEILING + 1)), save=True)

        with (
            mock.patch.object(FileSystemStorage, "open", _bounded_open),
            mock.patch("urbanlens.dashboard.services.media.stored_field.reencode_image_file") as reencode,
        ):
            outcome = reencode_stored_field(
                Label.objects.all(),
                label.pk,
                "custom_icon",
                label.custom_icon.name,
                max_dimension=256,
                convert_webp=True,
                max_bytes=_CEILING,
            )

        self.assertIs(outcome, Reencoded.TOO_LARGE)
        reencode.assert_not_called()
