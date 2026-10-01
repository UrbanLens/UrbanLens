"""``DashboardQuerySet.number_in_order`` writes raw SQL, so it refuses a field it cannot write that way."""

from __future__ import annotations

from unittest import mock

from django.db.models import CharField
from django.test import SimpleTestCase

from urbanlens.dashboard.models.wiki.model import Wiki


class NumberInOrderVersioningTests(SimpleTestCase):
    def test_a_versioned_field_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            Wiki.objects.all().number_in_order([1], field="name")

    def test_a_field_that_is_not_an_integer_column_is_refused(self) -> None:
        from urbanlens.dashboard.models.pin_list.model import PinListItem

        for field in ("pin", "added_via"):
            with self.subTest(field=field), self.assertRaises(TypeError):
                PinListItem.objects.all().number_in_order([1], field=field)

    def test_a_model_without_an_integer_key_is_refused(self) -> None:
        from urbanlens.dashboard.models.wiki.model import Wiki

        with mock.patch.object(Wiki._meta, "pk", CharField(name="code")), self.assertRaises(TypeError):
            Wiki.objects.all().number_in_order(["a"], field="id")
