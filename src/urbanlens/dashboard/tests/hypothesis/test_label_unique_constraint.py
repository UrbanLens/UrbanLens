"""Labels are unique per (lower(name), profile, kind), and duplicates merge cleanly.

``Label`` previously had no uniqueness at all, so nine `get_or_create` sites
treating `(profile, name, kind)` as identifying could each race into a duplicate.
Migration 0042 merges existing duplicates and adds the constraint.

These tests cover the three things that can go wrong with that change:

- the constraint exists and is case-insensitive, since callers already assumed
  case-insensitive identity (`media_labels.py` pre-filtered with `name__iexact`
  precisely because `get_or_create(name=...)` is not);
- global labels (``profile IS NULL``) are constrained against each other, which
  needs ``nulls_distinct=False`` - Postgres treats NULLs as distinct by default,
  so the obvious constraint would silently allow duplicate globals;
- the merge moves everything attached to the losing label rather than deleting
  it, which is the part a user would notice.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


def _migration_0042():
    """Load the module holding the 0042 merge logic, by path since it starts with a digit.

    The 0042 data migration was squashed into 0030_v0_7_0.py, which still
    defines ``_merge`` and ``_0042_merge_duplicate_labels`` under those names.
    """
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "migrations" / "0030_v0_7_0.py"
    spec = importlib.util.spec_from_file_location("migration_0042", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        """``profile IS NULL`` twice: Postgres treats NULLs as distinct unless the
        constraint says otherwise, so this is what ``nulls_distinct=False`` buys."""
        Label.objects.create(profile=None, name="ZzAudit Bridge", kind=KIND_TAG)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Label.objects.create(profile=None, name="zzaudit bridge", kind=KIND_TAG)

    def test_a_profile_may_still_hold_a_name_a_global_label_uses(self) -> None:
        """The constraint alone permits this - the profile values differ. Migration
        0042 merges the pre-existing ones, and the UI refuses to create new ones;
        the database is deliberately not the thing enforcing that."""
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


class LabelDuplicateMergeTests(TestCase):
    """The 0042 data pass, exercised by running its function against real rows.

    The constraint is already applied in the test database, so duplicates cannot
    be created through the ORM. These call the migration's own merge helper on
    rows inserted underneath it, which is the only way to reproduce what the
    beta databases actually contain.
    """

    def setUp(self) -> None:
        """Drop the constraint, then build fixtures under it.

        A duplicate cannot be produced through the ORM once 0042 has run - that
        is the point of the migration - so reproducing what the beta databases
        contain means removing the constraint first.

        It is a ``DROP INDEX``, not ``DROP CONSTRAINT``: Django implements an
        *expression*-based ``UniqueConstraint`` as a unique index, because
        Postgres cannot express ``lower(name)`` as a table constraint. Asking for
        a constraint by that name reports it does not exist, while ``\\d`` lists
        it plainly under Indexes.

        No restore is needed - Postgres DDL is transactional and ``TestCase``
        rolls the whole test back, so the drop is undone automatically.
        """
        super().setUp()
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("DROP INDEX uq_label_profile_name_kind_ci")

        self.profile = Profile.objects.get(user=baker.make(User))

    def _raw_label(self, *, name: str, kind: str = KIND_TAG, profile: Profile | None) -> int:
        """Create a label that duplicates an existing one (constraint must be dropped)."""
        return Label.objects.create(profile=profile, name=name, kind=kind).pk

    def _pin(self) -> Pin:
        return baker.make(Pin, profile=self.profile, location=baker.make(Location))

    def test_merging_moves_pins_and_deletes_the_duplicate(self) -> None:
        from django.db import connection

        migration = _migration_0042()
        keep = Label.objects.create(profile=self.profile, name="Keeper", kind=KIND_TAG)
        drop_id = self._raw_label(name="Keeper", profile=self.profile)
        pin = self._pin()
        Label.objects.get(pk=drop_id).pins.add(pin)

        with connection.cursor() as cursor:
            migration._merge(cursor, keep_id=keep.pk, drop_ids=[drop_id])

        self.assertFalse(Label.objects.filter(pk=drop_id).exists(), "the duplicate should be gone")
        self.assertIn(pin.pk, keep.pins.values_list("pk", flat=True), "the pin should have moved to the survivor")

    def test_a_pin_carrying_both_labels_does_not_break_the_move(self) -> None:
        """The through table is unique on (pin, label); a naive UPDATE would
        collide the moment the second row was repointed."""
        from django.db import connection

        migration = _migration_0042()
        keep = Label.objects.create(profile=self.profile, name="Both", kind=KIND_TAG)
        drop_id = self._raw_label(name="Both", profile=self.profile)
        pin = self._pin()
        pin.labels.add(keep, Label.objects.get(pk=drop_id))

        with connection.cursor() as cursor:
            migration._merge(cursor, keep_id=keep.pk, drop_ids=[drop_id])

        self.assertFalse(Label.objects.filter(pk=drop_id).exists())
        self.assertEqual(pin.labels.filter(pk=keep.pk).count(), 1, "the pin should carry the survivor exactly once")


class LabelConflictHandlingTests(TestCase):
    """A colliding name must produce a message, not an IntegrityError 500.

    The constraint alone turns a duplicate name into a database error, which
    reaches the user as a 500 with no indication of what went wrong. Every write
    path checks first.
    """

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
        """Matching the constraint - an exact-match-only check would let this
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
        """The least obvious collision: the name is unchanged, the *kind* moves.

        A tag called "Bridge" converted to a category collides with an existing
        category "Bridge" - so the check has to use the incoming ``new_kind``,
        not the label's current one.
        """
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
