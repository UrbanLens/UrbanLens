"""Private profile trust/nickname/label-toggle widgets (P37 - all three had zero test coverage)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_USER
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.labels.profile_assignment.model import ProfileLabelAssignment
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.profile.nickname import ProfileNickname
from urbanlens.dashboard.models.profile.trust import ProfileTrust


class _ProfileAnnotationCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.author = baker.make(User).profile
        self.subject = baker.make(User).profile
        self.client.force_login(self.author.user)
        # Annotating a profile needs the author to be able to see it.
        Profile.objects.filter(pk=self.subject.pk).update(profile_visibility=VisibilityChoice.ANYONE)


class TrustTests(_ProfileAnnotationCase):
    def _post(self, rating: object, subject=None):
        return self.client.post(
            reverse("profile.trust", args=[(subject or self.subject).slug]),
            {} if rating is None else {"rating": rating},
        )

    def test_sets_a_trust_rating(self) -> None:
        response = self._post(4)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProfileTrust.objects.for_pair(self.author, self.subject).first().rating, 4)

    def test_updates_an_existing_rating(self) -> None:
        ProfileTrust.objects.create(author=self.author, subject=self.subject, rating=2)

        self._post(5)

        ratings = ProfileTrust.objects.for_pair(self.author, self.subject)
        self.assertEqual(ratings.count(), 1)
        self.assertEqual(ratings.first().rating, 5)

    def test_clears_the_rating_when_posted_zero(self) -> None:
        ProfileTrust.objects.create(author=self.author, subject=self.subject, rating=3)

        self._post(0)

        self.assertFalse(ProfileTrust.objects.for_pair(self.author, self.subject).exists())

    def test_missing_rating_also_clears(self) -> None:
        ProfileTrust.objects.create(author=self.author, subject=self.subject, rating=3)

        self._post(None)

        self.assertFalse(ProfileTrust.objects.for_pair(self.author, self.subject).exists())

    def test_an_out_of_range_rating_clears_rather_than_erroring(self) -> None:
        """The widget posts an out-of-range value to mean "clear", not "reject" - see the view's own comment."""
        ProfileTrust.objects.create(author=self.author, subject=self.subject, rating=3)

        response = self._post(99)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ProfileTrust.objects.for_pair(self.author, self.subject).exists())

    def test_cannot_rate_your_own_profile(self) -> None:
        response = self._post(4, subject=self.author)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProfileTrust.objects.for_pair(self.author, self.author).exists())


class NicknameTests(_ProfileAnnotationCase):
    def _post(self, nickname: str, subject=None):
        return self.client.post(
            reverse("profile.nickname", args=[(subject or self.subject).slug]),
            {"nickname": nickname},
        )

    def test_sets_a_nickname(self) -> None:
        response = self._post("Ladder Guy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProfileNickname.objects.for_pair(self.author, self.subject).first().nickname, "Ladder Guy")

    def test_updates_an_existing_nickname(self) -> None:
        ProfileNickname.objects.create(author=self.author, subject=self.subject, nickname="Old Name")

        self._post("New Name")

        nicknames = ProfileNickname.objects.for_pair(self.author, self.subject)
        self.assertEqual(nicknames.count(), 1)
        self.assertEqual(nicknames.first().nickname, "New Name")

    def test_blank_nickname_clears_the_existing_one(self) -> None:
        ProfileNickname.objects.create(author=self.author, subject=self.subject, nickname="Old Name")

        self._post("   ")

        self.assertFalse(ProfileNickname.objects.for_pair(self.author, self.subject).exists())

    def test_cannot_nickname_your_own_profile(self) -> None:
        response = self._post("Me", subject=self.author)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProfileNickname.objects.for_pair(self.author, self.author).exists())

    def test_a_too_long_nickname_is_rejected_and_nothing_is_saved(self) -> None:
        response = self._post("x" * 101)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProfileNickname.objects.for_pair(self.author, self.subject).exists())


class LabelToggleTests(_ProfileAnnotationCase):
    def setUp(self) -> None:
        super().setUp()
        self.label = baker.make(Label, profile=self.author, kind=KIND_USER, name="Climbing Buddy")

    def _post(self, label, subject=None):
        return self.client.post(reverse("profile.label_toggle", args=[(subject or self.subject).slug, label.pk]))

    def test_toggles_the_label_on_then_off(self) -> None:
        first = self._post(self.label)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(
            ProfileLabelAssignment.objects.filter(author=self.author, subject=self.subject, label=self.label).exists(),
        )

        second = self._post(self.label)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(
            ProfileLabelAssignment.objects.filter(author=self.author, subject=self.subject, label=self.label).exists(),
        )

    def test_cannot_annotate_your_own_profile(self) -> None:
        response = self._post(self.label, subject=self.author)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProfileLabelAssignment.objects.filter(author=self.author, subject=self.author).exists())

    def test_a_label_owned_by_someone_else_is_not_reachable(self) -> None:
        other_author = baker.make(User).profile
        someone_elses_label = baker.make(Label, profile=other_author, kind=KIND_USER, name="Their Person")

        response = self._post(someone_elses_label)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(ProfileLabelAssignment.objects.filter(label=someone_elses_label).exists())

    def test_a_non_person_label_kind_is_refused(self) -> None:
        category_label = baker.make(Label, profile=self.author, kind=KIND_CATEGORY, name="Urbex")

        response = self._post(category_label)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(ProfileLabelAssignment.objects.filter(label=category_label).exists())
