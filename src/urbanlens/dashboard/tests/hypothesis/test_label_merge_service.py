"""Tests for ``services.labels.merge`` - the shared label-merge implementation.

These cover the three bugs the extraction fixed, which the controller versions
had no coverage for at all:

- the merge is now atomic, so a failure partway through rolls the whole thing
  back instead of leaving attachments split across a half-deleted label;
- children are reparented onto the target instead of being silently orphaned;
- wiki attachments move for every pin-style kind, not just categories.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_STATUS, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.labels.merge import LabelMergeError, merge_labels
from urbanlens.dashboard.services.pin_creation import create_pin_for_profile


class LabelMergeServiceTests(TestCase):
    """Behavior of ``merge_labels`` itself, independent of any view."""

    def setUp(self) -> None:
        baker.make(User)
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)

    def _label(self, name: str, kind: str = KIND_TAG, **kwargs) -> Label:
        return Label.objects.create(profile=self.profile, name=name, kind=kind, **kwargs)

    #: Incremented per pin so each gets distinct coordinates - a profile may
    #: not have two pins on one Location (db_pin_unique_location_per_profile).
    _pin_seq = 0

    def _pin(self, name: str):
        type(self)._pin_seq += 1
        offset = type(self)._pin_seq / 100
        return create_pin_for_profile(self.profile, name=name, latitude=40.0 + offset, longitude=-70.0 - offset).pin

    def test_pins_move_and_sources_are_deleted(self) -> None:
        target = self._label("Keep")
        source_a = self._label("A")
        source_b = self._label("B")
        pin_a, pin_b = self._pin("PA"), self._pin("PB")
        pin_a.labels.add(source_a)
        pin_b.labels.add(source_b)

        # Captured up front: Django clears the in-memory pk on delete, so
        # source_a.pk is None by the time merge_labels returns.
        source_ids = [source_a.pk, source_b.pk]

        result = merge_labels(target=target, sources=[source_a, source_b], profile=self.profile)

        self.assertEqual(result.target_id, target.pk)
        self.assertEqual(sorted(result.merged_ids), sorted(source_ids))
        self.assertEqual(result.pins_moved, 2)
        self.assertFalse(Label.objects.filter(pk__in=source_ids).exists())
        self.assertIn(target, pin_a.labels.all())
        self.assertIn(target, pin_b.labels.all())

    def test_pins_already_on_the_target_are_not_double_counted(self) -> None:
        target = self._label("Keep")
        source = self._label("Drop")
        pin = self._pin("P")
        pin.labels.add(target, source)

        result = merge_labels(target=target, sources=[source], profile=self.profile)
        self.assertEqual(result.pins_moved, 0)

    def test_children_are_reparented_rather_than_orphaned(self) -> None:
        """Bug 2: the controller versions dropped the whole subtree."""
        target = self._label("Keep")
        source = self._label("Drop")
        child = self._label("Child")
        grandchild = self._label("Grandchild")
        child.parents.add(source)
        grandchild.parents.add(child)

        merge_labels(target=target, sources=[source], profile=self.profile)

        child.refresh_from_db()
        self.assertEqual(list(child.parents.all()), [target])
        # The rest of the subtree is untouched.
        self.assertEqual(list(grandchild.parents.all()), [child])

    def test_a_target_that_was_a_child_of_the_source_is_not_made_its_own_parent(self) -> None:
        target = self._label("Keep")
        source = self._label("Drop")
        target.parents.add(source)

        merge_labels(target=target, sources=[source], profile=self.profile)

        target.refresh_from_db()
        self.assertNotIn(target, target.parents.all())
        self.assertEqual(target.parents.count(), 0)

    def test_merge_is_atomic_on_failure(self) -> None:
        """Bug 1: a mid-merge failure must leave nothing behind."""
        target = self._label("Keep")
        source = self._label("Drop")
        pin = self._pin("P")
        pin.labels.add(source)

        # Fail after the attachments have moved but before the delete commits.
        with patch("urbanlens.dashboard.services.labels.merge._reparent_children", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                merge_labels(target=target, sources=[source], profile=self.profile)

        # Everything rolled back: the source still exists and still owns its pin.
        self.assertTrue(Label.objects.filter(pk=source.pk).exists())
        self.assertIn(source, pin.labels.all())
        self.assertNotIn(target, pin.labels.all())

    def test_wikis_move_for_a_tag_not_only_a_category(self) -> None:
        """Bug 3: the multi-merge path used to drop these for non-categories."""
        target = self._label("Keep", kind=KIND_TAG)
        source = self._label("Drop", kind=KIND_TAG)
        wiki = baker.make("dashboard.Wiki")
        wiki.labels.add(source)

        merge_labels(target=target, sources=[source], profile=self.profile)
        self.assertIn(target, wiki.labels.all())

    def test_cross_kind_merge_is_refused(self) -> None:
        target = self._label("Tag", kind=KIND_TAG)
        source = self._label("Status", kind=KIND_STATUS)
        with self.assertRaises(LabelMergeError):
            merge_labels(target=target, sources=[source], profile=self.profile)
        self.assertTrue(Label.objects.filter(pk=source.pk).exists())

    def test_global_source_is_refused(self) -> None:
        target = self._label("Mine", kind=KIND_CATEGORY)
        shared = Label.objects.create(profile=None, name="Shared", kind=KIND_CATEGORY)
        with self.assertRaises(LabelMergeError):
            merge_labels(target=target, sources=[shared], profile=self.profile)
        self.assertTrue(Label.objects.filter(pk=shared.pk).exists())

    def test_another_users_source_is_refused(self) -> None:
        target = self._label("Mine")
        other = baker.make(User)
        theirs = Label.objects.create(profile=Profile.objects.get(user=other), name="Theirs", kind=KIND_TAG)
        with self.assertRaises(LabelMergeError):
            merge_labels(target=target, sources=[theirs], profile=self.profile)

    def test_protected_source_is_refused(self) -> None:
        target = self._label("Keep")
        protected = self._label("Visited", is_protected=True)
        with self.assertRaises(LabelMergeError):
            merge_labels(target=target, sources=[protected], profile=self.profile)

    def test_self_merge_is_refused(self) -> None:
        label = self._label("Solo")
        with self.assertRaises(LabelMergeError):
            merge_labels(target=label, sources=[label], profile=self.profile)

    def test_empty_sources_is_refused(self) -> None:
        target = self._label("Keep")
        with self.assertRaises(LabelMergeError):
            merge_labels(target=target, sources=[], profile=self.profile)

    def test_a_global_label_may_be_the_target(self) -> None:
        """Merging your own label into a shared one is normal cleanup."""
        shared = Label.objects.create(profile=None, name="Shared", kind=KIND_CATEGORY)
        mine = self._label("Mine", kind=KIND_CATEGORY)
        pin = self._pin("P")
        pin.labels.add(mine)

        merge_labels(target=shared, sources=[mine], profile=self.profile)
        self.assertIn(shared, pin.labels.all())
        self.assertFalse(Label.objects.filter(pk=mine.pk).exists())
