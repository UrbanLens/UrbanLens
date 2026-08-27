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

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment


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
        self.assertFalse(default_storage.exists(path), "the comment's photo outlived its row - an orphan the media gate serves to any logged-in user")

    def test_deleting_a_comment_without_an_image_is_unaffected(self) -> None:
        comment = baker.make(Comment, pin=self.pin, profile=self.profile, text="no photo here")

        response = self.client.delete(reverse("pin.comment.delete", args=[self.pin.slug, comment.pk]))

        self.assertIn(response.status_code, (200, 204))
        self.assertFalse(Comment.objects.filter(pk=comment.pk).exists())
