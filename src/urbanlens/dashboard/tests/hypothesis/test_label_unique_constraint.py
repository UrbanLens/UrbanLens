"""Labels are unique per (lower(name), profile, kind), and duplicates merge cleanly."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile


class LabelUniqueConstraintTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))
        self.other = Profile.objects.get(user=baker.make(User))

    def test_the_same_name_twice_for_one_profile_is_refused(self) -> None:
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)

    def test_the_check_is_case_insensitive(self) -> None:
        """Callers already assumed this - see media_labels.py's iexact pre-filter."""
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Label.objects.create(profile=self.profile, name="zzaudit abandoned", kind=KIND_TAG)

    def test_a_different_kind_is_a_different_label(self) -> None:
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_CATEGORY)

        self.assertEqual(Label.objects.filter(profile=self.profile, name__iexact="ZzAudit Abandoned").count(), 2)

    def test_another_profile_may_use_the_same_name(self) -> None:
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)
        Label.objects.create(profile=self.other, name="ZzAudit Abandoned", kind=KIND_TAG)

        self.assertEqual(Label.objects.filter(name__iexact="ZzAudit Abandoned", kind=KIND_TAG).count(), 2)

    def test_two_global_labels_cannot_share_a_name(self) -> None:
        """
        ``profile IS NULL`` twice: Postgres treats NULLs as distinct unless the
        constraint says otherwise, so this is what ``nulls_distinct=False`` buys."""
        Label.objects.create(profile=None, name="ZzAudit Bridge", kind=KIND_TAG)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Label.objects.create(profile=None, name="zzaudit bridge", kind=KIND_TAG)

    def test_a_profile_may_still_hold_a_name_a_global_label_uses(self) -> None:
        """The constraint alone permits this - the profile values differ."""
        Label.objects.create(profile=None, name="ZzAudit Bridge", kind=KIND_TAG)
        Label.objects.create(profile=self.profile, name="ZzAudit Bridge", kind=KIND_TAG)

        self.assertEqual(Label.objects.filter(name__iexact="ZzAudit Bridge", kind=KIND_TAG).count(), 2)

    def test_get_or_create_is_now_safe_to_repeat(self) -> None:
        """The nine call sites that assumed this now genuinely have it."""
        first, created_first = Label.objects.get_or_create(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG)
        second, created_second = Label.objects.get_or_create(
            profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)


class LabelConflictHandlingTests(TestCase):
    """
    A colliding name must produce a message, not an IntegrityError 500.

    The constraint alone turns a duplicate name into a database error, which reaches the user as a 500 with no
    indication of what went wrong."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def test_creating_a_duplicate_is_refused_with_a_message(self) -> None:
        from django.urls import reverse

        Label.objects.create(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG)

        response = self.client.post(reverse("label.create", kwargs={"label_kind": "tag"}), {"name": "zzaudit rooftop"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("already have", response.content.decode().lower())

    def test_the_refusal_is_case_insensitive(self) -> None:
        """
        Matching the constraint - an exact-match-only check would let this
        through and then fail at the database."""
        from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label

        Label.objects.create(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG)

        self.assertIsNotNone(find_conflicting_label(profile=self.profile, name="ZZAUDIT ROOFTOP", kind=KIND_TAG))

    def test_shadowing_a_global_label_is_refused(self) -> None:
        """Wider than the constraint: profile differs, so the database allows it."""
        from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label, label_conflict_message

        Label.objects.create(profile=None, name="ZzAudit Bridge", kind=KIND_TAG)

        conflict = find_conflicting_label(profile=self.profile, name="zzaudit bridge", kind=KIND_TAG)

        self.assertIsNotNone(conflict)
        self.assertIn("built-in", label_conflict_message(conflict, singular_title="Tag"))

    def test_renaming_a_label_to_its_own_name_is_allowed(self) -> None:
        """exclude_pk - otherwise every no-op save would report a self-collision."""
        from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label

        label = Label.objects.create(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG)

        self.assertIsNone(
            find_conflicting_label(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG, exclude_pk=label.pk)
        )

    def test_changing_only_the_case_of_a_name_is_allowed(self) -> None:
        from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label

        label = Label.objects.create(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG)

        self.assertIsNone(
            find_conflicting_label(profile=self.profile, name="ZZAUDIT ROOFTOP", kind=KIND_TAG, exclude_pk=label.pk)
        )

    def test_a_different_kind_does_not_collide(self) -> None:
        from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label

        Label.objects.create(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG)

        self.assertIsNone(find_conflicting_label(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_CATEGORY))

    def test_another_profiles_label_does_not_collide(self) -> None:
        from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label

        other = Profile.objects.get(user=baker.make(User))
        Label.objects.create(profile=other, name="ZzAudit Rooftop", kind=KIND_TAG)

        self.assertIsNone(find_conflicting_label(profile=self.profile, name="ZzAudit Rooftop", kind=KIND_TAG))

    def test_converting_a_kind_into_a_taken_name_is_refused(self) -> None:
        """
        The least obvious collision: the name is unchanged, the *kind* moves.

        A tag called "Bridge" converted to a category collides with an existing category "Bridge" - so the check
        has to use the incoming ``new_kind``, not the label's current one."""
        from urbanlens.dashboard.services.labels.uniqueness import find_conflicting_label

        tag = Label.objects.create(profile=self.profile, name="ZzAudit Bridge", kind=KIND_TAG)
        Label.objects.create(profile=self.profile, name="ZzAudit Bridge", kind=KIND_CATEGORY)

        self.assertIsNone(
            find_conflicting_label(profile=self.profile, name=tag.name, kind=KIND_TAG, exclude_pk=tag.pk),
            "staying a tag is fine - it is only itself",
        )
        self.assertIsNotNone(
            find_conflicting_label(profile=self.profile, name=tag.name, kind=KIND_CATEGORY, exclude_pk=tag.pk),
            "converting it to a category must collide with the existing category",
        )
