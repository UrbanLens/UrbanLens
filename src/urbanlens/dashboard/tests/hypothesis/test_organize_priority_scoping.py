"""Regression tests for the Display Order tab's cross-tenant write.

``OrganizePrioritySaveView`` validated the submitted label ids against
``Label.objects.visible_to(profile)`` - which deliberately spans a profile's own
labels *plus* the site-wide global ones - and then wrote ``order`` back with an
unscoped ``Label.objects.filter(id=item_id).update(...)``. Every user who
dragged a row on the Display Order tab was therefore rewriting the ordering of
shared global labels for everyone on the site, silently and with the last
writer winning.

The fix scopes the write to ``for_profile`` and reports the skipped ids rather
than pretending the gesture applied. These tests pin both halves: a global's
``order`` must survive another user's reorder, and the caller must be told which
ids were refused so the UI can lock those rows instead of losing the drag.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.labels import ensure_label
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import KIND_TAG, Label
from urbanlens.dashboard.models.profile.model import Profile


class OrganizePriorityScopingTests(TestCase):
    """The Display Order save must never write a label the caller does not own."""

    def setUp(self) -> None:
        """Create two users, one global label, and one owned label each."""
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.user = baker.make(User, username="dragger")
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User, username="bystander")
        self.other_profile = Profile.objects.get(user=self.other_user)

        # profile=None is what makes a label global/shared - see
        # LabelQuerySet.visible_to, which unions it with the caller's own.
        self.global_label = ensure_label(name="Abandoned", kind=KIND_TAG, profile=None, order=500)
        self.own_label = ensure_label(name="Mine", kind=KIND_TAG, profile=self.profile, order=1)
        self.other_label = ensure_label(name="Theirs", kind=KIND_TAG, profile=self.other_profile, order=2)

        self.client.force_login(self.user)

    def _save_order(self, *labels: Label):
        """POST the given labels as a display order, first item ranked highest."""
        return self.client.post(
            reverse("organize.priority.save"),
            data=json.dumps({"items": [{"id": label.pk} for label in labels]}),
            content_type="application/json",
        )

    def test_a_global_labels_order_survives_another_users_reorder(self) -> None:
        """The whole point: dragging must not renumber a label shared with everyone."""
        before = self.global_label.order

        response = self._save_order(self.global_label, self.own_label)

        self.assertEqual(response.status_code, 200)
        self.global_label.refresh_from_db()
        self.assertEqual(self.global_label.order, before)

    def test_the_callers_own_label_is_still_reordered(self) -> None:
        """Skipping globals must not turn the endpoint into a no-op."""
        response = self._save_order(self.global_label, self.own_label)

        self.assertEqual(response.status_code, 200)
        self.own_label.refresh_from_db()
        # Two submitted items, own_label second: order = total - index = 2 - 1.
        self.assertEqual(self.own_label.order, 1)

    def test_the_response_names_the_ids_it_refused(self) -> None:
        """A silently dropped id would read to the client as a successful drag."""
        response = self._save_order(self.global_label, self.own_label)

        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reordered"], 1)
        self.assertEqual(payload["skipped_global_ids"], [self.global_label.pk])

    def test_another_profiles_label_is_refused_like_a_global(self) -> None:
        """Someone else's private label was never visible_to the caller, but assert it anyway."""
        before = self.other_label.order

        response = self._save_order(self.other_label, self.own_label)

        self.assertEqual(response.status_code, 200)
        self.other_label.refresh_from_db()
        self.assertEqual(self.other_label.order, before)
        self.assertIn(self.other_label.pk, response.json()["skipped_global_ids"])

    def test_a_submission_of_only_globals_writes_nothing(self) -> None:
        """The degenerate case still has to answer 200 rather than error or write."""
        before = self.global_label.order

        response = self._save_order(self.global_label)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reordered"], 0)
        self.global_label.refresh_from_db()
        self.assertEqual(self.global_label.order, before)
