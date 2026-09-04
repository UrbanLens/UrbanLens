"""Deleting a comment removes its stored photo, not just its row.

Django stopped deleting ``FileField`` files on row deletion in 1.3, so every
deleted comment-with-photo used to strand its file under ``comment_images/``
- where the media gate's orphan branch serves it to any authenticated user
who knows the name (PROBLEMS.md, "Authenticated media gate - residual
per-family risk"). Each comment owns its file outright:
``attach_existing_comment_image`` copies rather than sharing storage, which is
what makes deleting the file safe.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.wiki.model import Wiki


class CommentImageCleanupTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)

    def _comment_with_image(self) -> Comment:
        comment = baker.make(Comment, pin=self.pin, profile=self.profile, text="look at this")
        comment.image.save("orphan_probe.jpg", ContentFile(b"not-a-real-jpeg"), save=True)
        return comment

    def test_deleting_a_comment_removes_its_file(self) -> None:
        comment = self._comment_with_image()
        path = comment.image.name
        self.assertTrue(default_storage.exists(path), "precondition: the file must exist before deletion")

        response = self.client.delete(reverse("pin.comment.delete", args=[self.pin.slug, comment.pk]))

        self.assertIn(response.status_code, (200, 204))
        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())
        self.assertFalse(
            default_storage.exists(path),
            "the comment's photo outlived its row - an orphan the media gate serves to any logged-in user",
        )
        # _discard_comment_image runs after comment.delete() has already nulled the
        # in-memory instance's pk; it must call image.delete(save=False) - a stray
        # save=True would call instance.save() on that pk-less object and silently
        # resurrect the row (as a new INSERT) instead of leaving it deleted.
        self.assertEqual(
            Comment.objects.count(), 0, "comment.image.delete() must not resurrect the row via instance.save()"
        )

    def test_deleting_a_comment_without_an_image_is_unaffected(self) -> None:
        comment = baker.make(Comment, pin=self.pin, profile=self.profile, text="no photo here")

        response = self.client.delete(reverse("pin.comment.delete", args=[self.pin.slug, comment.pk]))

        self.assertIn(response.status_code, (200, 204))
        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())

    def test_a_storage_error_while_removing_the_file_does_not_fail_the_delete(self) -> None:
        """_discard_comment_image is documented best-effort: the row is already gone by
        the time it runs, so a storage hiccup must not turn a successful delete into a 500."""
        comment = self._comment_with_image()
        comment_pk = comment.pk

        with patch("django.core.files.storage.default_storage.delete", side_effect=OSError("disk unavailable")):
            response = self.client.delete(reverse("pin.comment.delete", args=[self.pin.slug, comment.pk]))

        self.assertIn(response.status_code, (200, 204))
        self.assertFalse(Comment.objects.filter(pk=comment_pk).exists())

    def test_a_forbidden_delete_attempt_leaves_the_file_alone(self) -> None:
        """The profile check in PinCommentDeleteView runs before comment.delete()/
        _discard_comment_image() - a denied delete must not touch the stored file."""
        other_profile = baker.make(User).profile
        comment = baker.make(Comment, pin=self.pin, profile=other_profile, text="not mine")
        comment.image.save("forbidden_probe.jpg", ContentFile(b"not-a-real-jpeg"), save=True)
        path = comment.image.name

        response = self.client.delete(reverse("pin.comment.delete", args=[self.pin.slug, comment.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Comment.objects.filter(pk=comment.pk).exists())
        self.assertTrue(default_storage.exists(path), "a forbidden delete must not remove the file")


class WikiCommentImageCleanupTests(TestCase):
    """Same _discard_comment_image helper, exercised via WikiCommentDeleteView - a
    separate call site from PinCommentDeleteView that was previously untested."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude=41.0, longitude=-75.0)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        self.wiki = baker.make(Wiki, location=location)

    def test_deleting_a_wiki_comment_removes_its_file(self) -> None:
        comment = baker.make(Comment, wiki=self.wiki, pin=None, profile=self.profile, text="wiki photo comment")
        comment.image.save("wiki_orphan_probe.jpg", ContentFile(b"not-a-real-jpeg"), save=True)
        path = comment.image.name
        self.assertTrue(default_storage.exists(path), "precondition: the file must exist before deletion")

        response = self.client.delete(
            reverse(
                "location.wiki.comment.delete",
                kwargs={"location_slug": self.wiki.location.slug, "comment_id": comment.pk},
            )
        )

        self.assertIn(response.status_code, (200, 204))
        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())
        self.assertFalse(default_storage.exists(path), "the wiki comment's photo outlived its row")
