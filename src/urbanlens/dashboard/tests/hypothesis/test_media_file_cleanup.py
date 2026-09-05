"""Replacing or deleting an icon must not leave its file on disk.

Django has not deleted `FileField` files since 1.3, so two ordinary actions
stranded one every time: replacing an icon or avatar with a new upload, and
deleting the row that named it. Small files, but they accumulate with normal
use - and a stranded file is exactly the "orphan" `services/media/access.py` had
to start refusing to serve, because an orphan cannot be told from a live file
whose owner the viewer may not learn about.

The ordering is what these pin hardest. The old file is read before the write
and deleted after it succeeds, never before: a failed write or a rolled-back
transaction must not leave a row pointing at a file that is gone. And a save
that does not touch the file column must not delete anything, which is the
mistake a naive `pre_save` delete makes on every unrelated edit.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile

#: The smallest thing Pillow will accept as a PNG, so `ImageField` validation
#: is not what this ends up testing.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4946485200000001000000010802000000907753"
    "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082",
)


def _icon(name: str = "icon.png") -> ContentFile:
    return ContentFile(_PNG, name=name)


class ReplacedFileCleanupTests(TestCase):
    """The previous file goes when a new one takes its place."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))
        self.label = Label.objects.create(name="Cleanup", kind=KIND_TAG, profile=self.profile)
        self.label.custom_icon = _icon("first.png")
        self.label.save()
        self.first = self.label.custom_icon.name

    def _exists(self, name: str) -> bool:
        return self.label.custom_icon.storage.exists(name)

    def test_the_replaced_file_is_deleted(self) -> None:
        self.assertTrue(self._exists(self.first))

        self.label.custom_icon = _icon("second.png")
        self.label.save()

        self.assertNotEqual(self.label.custom_icon.name, self.first)
        self.assertFalse(self._exists(self.first), "the replaced icon is still on disk")
        self.assertTrue(self._exists(self.label.custom_icon.name), "the new icon must survive")

    def test_clearing_the_field_deletes_the_file(self) -> None:
        self.label.custom_icon = None
        self.label.save()
        self.assertFalse(self._exists(self.first))

    def test_a_save_that_does_not_touch_the_icon_keeps_it(self) -> None:
        """The mistake a naive pre_save delete makes on every unrelated edit."""
        self.label.name = "Renamed"
        self.label.save()
        self.assertTrue(self._exists(self.first))

    def test_an_update_fields_save_elsewhere_keeps_it(self) -> None:
        self.label.name = "Renamed again"
        self.label.save(update_fields=["name", "updated"])
        self.assertTrue(self._exists(self.first))

    def test_creating_a_row_deletes_nothing(self) -> None:
        other = Label.objects.create(name="Fresh", kind=KIND_TAG, profile=self.profile, custom_icon=_icon("third.png"))
        self.assertTrue(self._exists(self.first))
        self.assertTrue(self._exists(other.custom_icon.name))


class DeletedRowFileCleanupTests(TestCase):
    """The file goes when its row does."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))

    def test_deleting_a_label_deletes_its_icon(self) -> None:
        label = Label.objects.create(name="Doomed", kind=KIND_TAG, profile=self.profile, custom_icon=_icon())
        stored, storage = label.custom_icon.name, label.custom_icon.storage
        self.assertTrue(storage.exists(stored))

        label.delete()

        self.assertFalse(storage.exists(stored), "deleting the row left its icon behind")

    def test_deleting_a_row_without_an_icon_is_fine(self) -> None:
        label = Label.objects.create(name="Plain", kind=KIND_TAG, profile=self.profile)
        label.delete()
        self.assertFalse(Label.objects.filter(name="Plain").exists())

    def test_a_queryset_delete_removes_the_files_too(self) -> None:
        """`QuerySet.delete()` still sends post_delete for each row."""
        label = Label.objects.create(name="Bulk", kind=KIND_TAG, profile=self.profile, custom_icon=_icon())
        stored, storage = label.custom_icon.name, label.custom_icon.storage

        Label.objects.filter(pk=label.pk).delete()

        self.assertFalse(storage.exists(stored))

    def test_a_missing_file_does_not_break_the_delete(self) -> None:
        """Re-running a cleanup, or a file removed by hand, must not raise."""
        label = Label.objects.create(name="Already gone", kind=KIND_TAG, profile=self.profile, custom_icon=_icon())
        label.custom_icon.storage.delete(label.custom_icon.name)

        label.delete()

        self.assertFalse(Label.objects.filter(name="Already gone").exists())


class AvatarCleanupTests(TestCase):
    """The same rule reaches `Profile.avatar`, which is not an icon."""

    def test_a_replaced_avatar_is_deleted(self) -> None:
        profile = Profile.objects.get(user=baker.make(User))
        profile.avatar = _icon("avatar-one.png")
        profile.save()
        first = profile.avatar.name

        profile.avatar = _icon("avatar-two.png")
        profile.save()

        self.assertFalse(profile.avatar.storage.exists(first))
        self.assertTrue(profile.avatar.storage.exists(profile.avatar.name))
