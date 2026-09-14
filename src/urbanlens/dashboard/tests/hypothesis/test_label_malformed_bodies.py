"""Malformed label JSON bodies and out-of-range orders are refused or bounded, never a 500.

``json.loads`` accepts the bare ``Infinity`` literal (``int(float("inf"))`` raises ``OverflowError``) and
any top-level JSON value, and ``Label.order`` is a 32-bit column that a 30-digit integer overflows on save.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label

_HUGE = "9" * 30
_INT32_MAX = 2**31 - 1


class _LabelCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.label = baker.make(Label, profile=self.profile, kind="tag", name="Zz Existing")

    def _json(self, route: str, body: str):
        return self.client.post(reverse(route, kwargs={"label_kind": "tag"}), body, content_type="application/json")


class BulkEditBodyTests(_LabelCase):
    def test_a_body_that_is_not_an_object(self) -> None:
        self.assertEqual(self._json("label.bulk_edit", "[1, 2]").status_code, 400)

    def test_an_infinite_id(self) -> None:
        self.assertEqual(self._json("label.bulk_edit", '{"ids": [Infinity]}').status_code, 400)

    def test_an_infinite_order(self) -> None:
        response = self._json("label.bulk_edit", json.dumps({"ids": [self.label.pk]})[:-1] + ', "order": Infinity}')

        self.assertLess(response.status_code, 500)

    def test_an_order_beyond_the_column_is_bounded(self) -> None:
        response = self._json("label.bulk_edit", f'{{"ids": [{self.label.pk}], "order": {_HUGE}}}')

        self.assertLess(response.status_code, 500)
        self.label.refresh_from_db()
        self.assertLessEqual(self.label.order, _INT32_MAX)


class ReorderBodyTests(_LabelCase):
    def test_an_infinite_id(self) -> None:
        self.assertEqual(self._json("label.reorder", '{"tag_ids": [Infinity]}').status_code, 400)

    def test_ids_that_are_not_a_list(self) -> None:
        self.assertEqual(self._json("label.reorder", '{"tag_ids": 5}').status_code, 400)


class FormOrderTests(_LabelCase):
    def test_creating_with_an_order_beyond_the_column(self) -> None:
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "tag"}), {"name": "Zz Huge", "order": _HUGE}
        )

        self.assertLess(response.status_code, 500)
        created = Label.objects.filter(profile=self.profile, name="Zz Huge").first()
        if created is not None:
            self.assertLessEqual(created.order, _INT32_MAX)

    def test_creating_with_a_non_numeric_parent(self) -> None:
        response = self.client.post(
            reverse("label.create", kwargs={"label_kind": "tag"}), {"name": "Zz Child", "parent_ids": ["abc"]}
        )

        self.assertLess(response.status_code, 500)

    def test_editing_with_a_non_numeric_parent(self) -> None:
        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tag", "label_id": self.label.pk}),
            {"name": self.label.name, "parent_ids": ["abc"]},
        )

        self.assertLess(response.status_code, 500)

    def test_a_real_parent_is_still_set_alongside_a_bad_one(self) -> None:
        parent = baker.make(Label, profile=self.profile, kind="tag", name="Zz Parent")

        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tag", "label_id": self.label.pk}),
            {"name": self.label.name, "parent_ids": [str(parent.pk), "abc"]},
        )

        self.assertLess(response.status_code, 500)
        self.assertEqual(list(self.label.parents.all()), [parent])

    def test_editing_with_an_order_beyond_the_column(self) -> None:
        response = self.client.post(
            reverse("label.edit", kwargs={"label_kind": "tag", "label_id": self.label.pk}),
            {"name": self.label.name, "order": _HUGE},
        )

        self.assertLess(response.status_code, 500)
        self.label.refresh_from_db()
        self.assertLessEqual(self.label.order, _INT32_MAX)
