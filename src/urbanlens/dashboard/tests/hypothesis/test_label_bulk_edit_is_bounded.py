"""The label bulk editor lets the request choose how much work it is.

`ids`, `add_parent_ids` and `add_child_ids` all arrive as client-supplied JSON
lists with no ceiling. Every (label, parent) pair then runs `_would_create_cycle`,
which walks the label graph in the database, and every label saved fires the
receiver that touches every pin carrying it - so the cost is the product of two
numbers the caller picks (N21 H26).

Refused rather than trimmed, unlike the *filter* ceilings: a bulk edit that
silently applied to some of the labels somebody selected is worse than one that
says no. The ceiling is far above what the Organize page's select-all produces.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile

SETTING_NAME = "LABEL_BULK_EDIT_MAX_IDS"


class _BulkEditCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.url = reverse("label.bulk_edit", kwargs={"label_kind": "tags"})

    def _labels(self, count: int) -> list[Label]:
        return [baker.make(Label, profile=self.profile, kind=KIND_TAG) for _ in range(count)]

    def _post(self, body: dict):
        return self.client.post(self.url, data=json.dumps(body), content_type="application/json")


class TheSettingExistsTests(_BulkEditCase):
    def test_the_ceiling_is_a_real_setting(self) -> None:
        self.assertTrue(hasattr(settings, SETTING_NAME), f"nothing reads {SETTING_NAME}")


class TooManyIdsAreRefusedTests(_BulkEditCase):
    @override_settings(**{SETTING_NAME: 3})
    def test_an_over_long_id_list_is_refused(self) -> None:
        response = self._post({"ids": [label.pk for label in self._labels(10)], "color": "#ff0000"})

        self.assertEqual(response.status_code, 400)

    @override_settings(**{SETTING_NAME: 3})
    def test_an_over_long_parent_list_is_refused(self) -> None:
        labels = self._labels(2)
        parents = self._labels(10)

        response = self._post({"ids": [labels[0].pk], "add_parent_ids": [parent.pk for parent in parents]})

        self.assertEqual(response.status_code, 400)

    @override_settings(**{SETTING_NAME: 3})
    def test_an_over_long_child_list_is_refused(self) -> None:
        labels = self._labels(2)
        children = self._labels(10)

        response = self._post({"ids": [labels[0].pk], "add_child_ids": [child.pk for child in children]})

        self.assertEqual(response.status_code, 400)

    @override_settings(**{SETTING_NAME: 3})
    def test_nothing_is_written_when_the_request_is_refused(self) -> None:
        """A refusal that had already saved half the labels would be the worst of both."""
        labels = self._labels(10)

        self._post({"ids": [label.pk for label in labels], "color": "#ff0000"})

        for label in labels:
            label.refresh_from_db()
            self.assertNotEqual(label.color, "#ff0000")


class AnOrdinaryBulkEditStillWorksTests(_BulkEditCase):
    """The half that stops the tests above passing against an editor that refuses everything."""

    @override_settings(**{SETTING_NAME: 50})
    def test_a_normal_selection_is_applied(self) -> None:
        labels = self._labels(4)

        response = self._post({"ids": [label.pk for label in labels], "color": "#ff0000"})

        self.assertEqual(response.status_code, 200)
        for label in labels:
            label.refresh_from_db()
            self.assertEqual(label.color, "#ff0000")

    @override_settings(**{SETTING_NAME: 50})
    def test_a_normal_parent_assignment_is_applied(self) -> None:
        child, parent = self._labels(2)

        response = self._post({"ids": [child.pk], "add_parent_ids": [parent.pk]})

        self.assertEqual(response.status_code, 200)
        self.assertIn(parent, child.parents.all())
