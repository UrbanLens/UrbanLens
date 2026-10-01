"""The label reorder had a ceiling on one of its two doors.

`LabelReorderSerializer` caps `uuids` at a literal 1000 and its docstring says it
mirrors `OrganizePrioritySaveView`'s `items` semantics. The view it mirrors has no
ceiling at all, so the internal door - the one the application's own UI posts to -
takes a list of any length.

Two things scale with that list, and neither is bounded by what the caller owns:

- `Label.objects.for_profile(profile).filter(id__in=item_ids)` carries every id
  submitted into one statement. A million ids is a multi-megabyte query for
  Postgres to parse and plan, on a connection held for the duration.
- `skipped_global_ids` collects every id that did not resolve and returns them
  all, so the response is as large as the request.

The fix is not a second literal. Two doors onto one action with two independently
written ceilings is how they drift, and this pair has already drifted once. Both
now read `LABEL_REORDER_MAX_IDS`, and the test below asserts they agree rather
than trusting that whoever edits one remembers the other.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.external_api.serializers_labels_bulk import LabelReorderSerializer
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label

SETTING = "LABEL_REORDER_MAX_IDS"


class TheCeilingIsARealSettingTests(TestCase):
    def test_the_ceiling_exists(self) -> None:
        """Overriding a name production lacks would pass against unbounded code."""
        self.assertTrue(hasattr(settings, SETTING), f"nothing defines {SETTING}")

    def test_it_is_a_positive_number(self) -> None:
        self.assertGreater(getattr(settings, SETTING, 0), 0)


class _ReorderCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _labels(self, count: int) -> list[Label]:
        return [baker.make(Label, profile=self.profile, kind=KIND_TAG, name=f"tag-{index}") for index in range(count)]

    def _post(self, ids: list[int]):
        return self.client.post(
            reverse("organize.priority.save"),
            data=json.dumps({"items": [{"id": value} for value in ids]}),
            content_type="application/json",
        )


class TheInternalDoorIsBoundedTests(_ReorderCase):
    @override_settings(**{SETTING: 5})
    def test_a_list_past_the_ceiling_is_refused(self) -> None:
        labels = self._labels(3)
        orders = [label.order for label in labels]

        response = self._post([label.pk for label in labels] + list(range(9000, 9020)))

        self.assertEqual(response.status_code, 400)
        for label, order in zip(labels, orders, strict=True):
            label.refresh_from_db()
            self.assertEqual(label.order, order, "a refused reorder wrote anyway")

    @override_settings(**{SETTING: 5})
    def test_a_list_under_the_ceiling_still_reorders(self) -> None:
        """The half that stops the test above passing against a view that refuses everything."""
        labels = self._labels(3)

        response = self._post([label.pk for label in labels])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reordered"], 3)

    @override_settings(**{SETTING: 5})
    def test_the_refusal_does_not_echo_the_submitted_ids_back(self) -> None:
        """Returning them all makes the response as large as the attack."""
        response = self._post(list(range(9000, 9100)))

        self.assertEqual(response.status_code, 400)
        self.assertLess(len(response.content), 2000, "the refusal carried the submitted list back out")


class BothDoorsShareOneCeilingTests(TestCase):
    """The divergence is the bug; a literal on either side would reintroduce it."""

    @override_settings(**{SETTING: 7})
    def test_the_api_serializer_reads_the_setting(self) -> None:
        serializer = LabelReorderSerializer(
            data={"uuids": [f"00000000-0000-4000-8000-{index:012d}" for index in range(8)]}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("uuids", serializer.errors)

    @override_settings(**{SETTING: 7})
    def test_the_api_serializer_still_accepts_a_list_under_it(self) -> None:
        serializer = LabelReorderSerializer(
            data={"uuids": [f"00000000-0000-4000-8000-{index:012d}" for index in range(7)]}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
