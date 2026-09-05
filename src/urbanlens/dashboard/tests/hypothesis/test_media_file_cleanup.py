"""Replacing or deleting an icon must not leave its file on disk.

Django has not deleted `FileField` files since 1.3, so two ordinary actions
stranded one every time: replacing an icon or avatar with a new upload, and
deleting the row that named it. Small files, but they accumulate with normal
use - and a stranded file is exactly the "orphan" `services/media/access.py` had
to start refusing to serve, because an orphan cannot be told from a live file
whose owner the viewer may not learn about.

Three things these pin hardest, each of which the obvious implementation gets
wrong:

- **The unlink waits for the commit.** `post_save`/`post_delete` fire inside the
  transaction, so deleting there survives a rollback that put the row back.
- **A save that does not touch the column deletes nothing** - the mistake a
  naive `pre_save` delete makes on every unrelated edit.
- **`Pin` and `Label` are not managed at all.** The undo framework stashes their
  icon as a stored *name*, so unlinking it would make an undo within the window
  restore a row pointing at nothing.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement
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
        self.achievement = baker.make(Achievement)
        with self.captureOnCommitCallbacks(execute=True):
            self.achievement.custom_icon = _icon("first.png")
            self.achievement.save()
        self.first = self.achievement.custom_icon.name

    def _exists(self, name: str) -> bool:
        return self.achievement.custom_icon.storage.exists(name)

    def test_the_replaced_file_is_deleted(self) -> None:
        self.assertTrue(self._exists(self.first))

        with self.captureOnCommitCallbacks(execute=True):
            self.achievement.custom_icon = _icon("second.png")
            self.achievement.save()

        self.assertNotEqual(self.achievement.custom_icon.name, self.first)
        self.assertFalse(self._exists(self.first), "the replaced icon is still on disk")
        self.assertTrue(self._exists(self.achievement.custom_icon.name), "the new icon must survive")

    def test_clearing_the_field_deletes_the_file(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            self.achievement.custom_icon = None
            self.achievement.save()
        self.assertFalse(self._exists(self.first))

    def test_a_save_that_does_not_touch_the_icon_keeps_it(self) -> None:
        """The mistake a naive pre_save delete makes on every unrelated edit."""
        with self.captureOnCommitCallbacks(execute=True):
            self.achievement.name = "Renamed"
            self.achievement.save()
        self.assertTrue(self._exists(self.first))

    def test_an_update_fields_save_elsewhere_keeps_it(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            self.achievement.name = "Renamed again"
            self.achievement.save(update_fields=["name", "updated"])
        self.assertTrue(self._exists(self.first))

    def test_creating_a_row_deletes_nothing(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            other = baker.make(Achievement, custom_icon=_icon("third.png"))
        self.assertTrue(self._exists(self.first))
        self.assertTrue(self._exists(other.custom_icon.name))


class DeletedRowFileCleanupTests(TestCase):
    """The file goes when its row does."""

    def _achievement(self, name: str, *, with_icon: bool = True):
        with self.captureOnCommitCallbacks(execute=True):
            return baker.make(Achievement, name=name, custom_icon=_icon() if with_icon else None)

    def test_deleting_a_row_deletes_its_icon(self) -> None:
        achievement = self._achievement("Doomed")
        stored, storage = achievement.custom_icon.name, achievement.custom_icon.storage
        self.assertTrue(storage.exists(stored))

        with self.captureOnCommitCallbacks(execute=True):
            achievement.delete()

        self.assertFalse(storage.exists(stored), "deleting the row left its icon behind")

    def test_deleting_a_row_without_an_icon_is_fine(self) -> None:
        achievement = self._achievement("Plain", with_icon=False)
        with self.captureOnCommitCallbacks(execute=True):
            achievement.delete()
        self.assertFalse(Achievement.objects.filter(name="Plain").exists())

    def test_a_queryset_delete_removes_the_files_too(self) -> None:
        """`QuerySet.delete()` still sends post_delete for each row."""
        achievement = self._achievement("Bulk")
        stored, storage = achievement.custom_icon.name, achievement.custom_icon.storage

        with self.captureOnCommitCallbacks(execute=True):
            Achievement.objects.filter(pk=achievement.pk).delete()

        self.assertFalse(storage.exists(stored))

    def test_a_missing_file_does_not_break_the_delete(self) -> None:
        """Re-running a cleanup, or a file removed by hand, must not raise."""
        achievement = self._achievement("Already gone")
        achievement.custom_icon.storage.delete(achievement.custom_icon.name)

        with self.captureOnCommitCallbacks(execute=True):
            achievement.delete()

        self.assertFalse(Achievement.objects.filter(name="Already gone").exists())

    def test_an_undo_restorable_model_keeps_its_icon(self) -> None:
        """`Pin` and `Label` are deliberately not managed.

        Undo stashes their icon as a stored *name*, so unlinking it would leave
        an undo within the window restoring a row that points at nothing.
        """
        from urbanlens.dashboard.models.labels.meta import KIND_TAG
        from urbanlens.dashboard.models.labels.model import Label

        profile = Profile.objects.get(user=baker.make(User))
        label = Label.objects.create(name="Undoable", kind=KIND_TAG, profile=profile, custom_icon=_icon())
        stored, storage = label.custom_icon.name, label.custom_icon.storage

        with self.captureOnCommitCallbacks(execute=True):
            label.delete()

        self.assertTrue(storage.exists(stored), "an undo could still restore a row naming this file")


class AvatarCleanupTests(TestCase):
    """The same rule reaches `Profile.avatar`, which is not an icon."""

    def test_a_replaced_avatar_is_deleted(self) -> None:
        profile = Profile.objects.get(user=baker.make(User))
        with self.captureOnCommitCallbacks(execute=True):
            profile.avatar = _icon("avatar-one.png")
            profile.save()
        first = profile.avatar.name

        with self.captureOnCommitCallbacks(execute=True):
            profile.avatar = _icon("avatar-two.png")
            profile.save()

        self.assertFalse(profile.avatar.storage.exists(first))
        self.assertTrue(profile.avatar.storage.exists(profile.avatar.name))

    def test_nothing_is_deleted_when_the_transaction_rolls_back(self) -> None:
        """The reason every unlink waits for the commit."""
        profile = Profile.objects.get(user=baker.make(User))
        with self.captureOnCommitCallbacks(execute=True):
            profile.avatar = _icon("keep-me.png")
            profile.save()
        first = profile.avatar.name

        # Captured and *not* executed: the same state a rollback leaves.
        with self.captureOnCommitCallbacks(execute=False):
            profile.avatar = _icon("replacement.png")
            profile.save()

        self.assertTrue(profile.avatar.storage.exists(first), "a rollback would have left the row pointing at this")
