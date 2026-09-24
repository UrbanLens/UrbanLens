"""Private profile notes: create/edit/delete (P37 - ProfileNoteView/Edit/Delete had zero test coverage)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.profile.note import ProfileNote


class _ProfileNoteCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.author = baker.make(User).profile
        self.subject = baker.make(User).profile
        self.client.force_login(self.author.user)
        # Annotating a profile needs the author to be able to see it.
        Profile.objects.filter(pk=self.subject.pk).update(profile_visibility=VisibilityChoice.ANYONE)

    def _create(self, content: str, subject=None):
        return self.client.post(
            reverse("profile.note", args=[(subject or self.subject).slug]),
            {"content": content},
        )

    def _edit(self, note: ProfileNote, content: str, subject=None):
        return self.client.post(
            reverse("profile.note.edit", args=[(subject or self.subject).slug, note.pk]),
            {"content": content},
        )

    def _delete(self, note: ProfileNote, subject=None):
        return self.client.post(reverse("profile.note.delete", args=[(subject or self.subject).slug, note.pk]))


class CreateTests(_ProfileNoteCase):
    def test_creates_a_note_with_the_posted_content(self) -> None:
        response = self._create("Seemed nervous about the fence line.")

        self.assertEqual(response.status_code, 200)
        note = ProfileNote.objects.get(author=self.author, subject=self.subject)
        self.assertEqual(note.content, "Seemed nervous about the fence line.")

    def test_blank_content_creates_nothing(self) -> None:
        response = self._create("   ")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProfileNote.objects.filter(author=self.author, subject=self.subject).exists())

    def test_cannot_annotate_your_own_profile(self) -> None:
        response = self._create("Note to self.", subject=self.author)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProfileNote.objects.filter(author=self.author).exists())


class EditTests(_ProfileNoteCase):
    def setUp(self) -> None:
        super().setUp()
        self.note = ProfileNote.objects.create(author=self.author, subject=self.subject, content="Original.")

    def test_edits_the_authors_own_note(self) -> None:
        response = self._edit(self.note, "Revised after a second visit.")

        self.assertEqual(response.status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.content, "Revised after a second visit.")

    def test_cannot_edit_another_authors_note_about_the_same_subject(self) -> None:
        other_author = baker.make(User).profile
        others_note = ProfileNote.objects.create(author=other_author, subject=self.subject, content="Not yours.")

        response = self._edit(others_note, "Overwritten!")

        self.assertEqual(response.status_code, 200)  # re-renders the (unaffected) partial, not a 404
        others_note.refresh_from_db()
        self.assertEqual(others_note.content, "Not yours.")

    def test_cannot_reach_a_note_about_a_different_subject_via_the_wrong_url(self) -> None:
        other_subject = baker.make(User).profile
        note_about_other = ProfileNote.objects.create(author=self.author, subject=other_subject, content="Elsewhere.")

        # note_id is real and belongs to this author - but the subject in the URL doesn't match.
        response = self._edit(note_about_other, "Overwritten!", subject=self.subject)

        self.assertEqual(response.status_code, 200)
        note_about_other.refresh_from_db()
        self.assertEqual(note_about_other.content, "Elsewhere.")


class DeleteTests(_ProfileNoteCase):
    def setUp(self) -> None:
        super().setUp()
        self.note = ProfileNote.objects.create(author=self.author, subject=self.subject, content="Delete me.")

    def test_deletes_the_authors_own_note(self) -> None:
        response = self._delete(self.note)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProfileNote.objects.filter(pk=self.note.pk).exists())

    def test_cannot_delete_another_authors_note_about_the_same_subject(self) -> None:
        other_author = baker.make(User).profile
        others_note = ProfileNote.objects.create(author=other_author, subject=self.subject, content="Not yours.")

        response = self._delete(others_note)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(ProfileNote.objects.filter(pk=others_note.pk).exists())

    def test_cannot_delete_a_note_about_a_different_subject_via_the_wrong_url(self) -> None:
        other_subject = baker.make(User).profile
        note_about_other = ProfileNote.objects.create(author=self.author, subject=other_subject, content="Elsewhere.")

        # note_id is real and belongs to this author - but the subject in the URL doesn't match.
        response = self._delete(note_about_other, subject=self.subject)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(ProfileNote.objects.filter(pk=note_about_other.pk).exists())
