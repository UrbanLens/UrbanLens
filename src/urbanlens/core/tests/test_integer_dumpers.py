"""An integer past its column is refused by the database layer, not stored as its low bits.

psycopg-binary's C dumpers for ``Int2`` and ``Int4`` - what Django sends an ``IntegerField`` as - keep only the low
16 or 32 bits, so with server-side binding 2**31 was stored as -2**31 and 2**32 + 1 as 1, and nothing raised.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import DataError, connection, transaction
from model_bakery import baker
from psycopg.adapt import PyFormat
from psycopg.types.numeric import Int2, Int4, Int8

from urbanlens.core.integer_dumpers import CheckedInt2BinaryDumper, CheckedInt4BinaryDumper, CheckedInt8BinaryDumper
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import TOTPDevice
from urbanlens.dashboard.models.custom_fields.model import CustomField
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label


class IntegerColumnWriteTests(TestCase):
    def assert_refused(self, write) -> None:
        with self.assertRaises(DataError), transaction.atomic():
            write()

    def test_an_integer_past_its_column_is_refused_and_the_row_kept(self) -> None:
        label = baker.make(Label, name="Wrap", kind=KIND_TAG, order=7)

        for value in (2**31, 2**32 + 1, -(2**31) - 1, 2**63):
            with self.subTest(value=value):
                self.assert_refused(lambda value=value: Label.objects.filter(pk=label.pk).update(order=value))
        label.refresh_from_db()
        self.assertEqual(label.order, 7)

    def test_a_smallint_past_its_column_is_refused(self) -> None:
        field = baker.make(CustomField, order=3)

        for value in (2**15, 2**16 + 3):
            with self.subTest(value=value):
                self.assert_refused(lambda value=value: CustomField.objects.filter(pk=field.pk).update(order=value))
        field.refresh_from_db()
        self.assertEqual(field.order, 3)

    def test_a_bigint_past_its_column_is_a_data_error_not_an_overflow(self) -> None:
        device = baker.make(TOTPDevice, user=baker.make(User), last_used_step=1)

        self.assert_refused(lambda: TOTPDevice.objects.filter(pk=device.pk).update(last_used_step=2**63))

    def test_the_column_extremes_are_stored_exactly(self) -> None:
        label = baker.make(Label, name="Edge", kind=KIND_TAG, order=0)

        for value in (2**31 - 1, -(2**31)):
            with self.subTest(value=value):
                Label.objects.filter(pk=label.pk).update(order=value)
                label.refresh_from_db()
                self.assertEqual(label.order, value)

    def test_every_connection_dumps_through_the_checked_dumpers(self) -> None:
        connection.ensure_connection()
        adapters = connection.connection.adapters

        self.assertIs(adapters.get_dumper(Int2, PyFormat.BINARY), CheckedInt2BinaryDumper)
        self.assertIs(adapters.get_dumper(Int4, PyFormat.BINARY), CheckedInt4BinaryDumper)
        self.assertIs(adapters.get_dumper(Int8, PyFormat.BINARY), CheckedInt8BinaryDumper)
