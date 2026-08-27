"""``Image.exif_data`` is encrypted at rest, and is now the only copy.

The upload pipeline strips the EXIF block out of the stored file, so this column
holds what the photo no longer carries. That makes two things matter more than
they would for an ordinary encrypted field: the plaintext must genuinely not be
in the database, and a key mismatch must not destroy the row - there is nothing
to re-derive it from once the file has been scrubbed.
"""

from __future__ import annotations

from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.fields import UndecryptableJSON
from urbanlens.dashboard.models.images.model import Image

_SAMPLE = {"Make": "ACME Cameras", "Model": "Nosy 9000", "GPSInfo": {"1": "N"}}


def _raw_column(pk: int) -> str | None:
    """The stored bytes, straight from the database, bypassing the field."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT exif_data FROM dashboard_images WHERE id = %s", [pk])
        return cursor.fetchone()[0]


class EncryptedExifDataTests(TestCase):
    def test_a_dict_survives_the_round_trip(self) -> None:
        image = baker.make(Image, image=None, exif_data=_SAMPLE)

        image.refresh_from_db()

        self.assertEqual(image.exif_data, _SAMPLE)

    def test_the_plaintext_is_not_in_the_database(self) -> None:
        """The whole point - a database read must not hand over the camera or the GPS."""
        image = baker.make(Image, image=None, exif_data=_SAMPLE)

        stored = _raw_column(image.pk)

        self.assertIsNotNone(stored)
        self.assertNotIn("ACME Cameras", stored)
        self.assertNotIn("GPSInfo", stored)
        self.assertTrue(stored.startswith("gAAAA"), "the column does not hold a Fernet token")

    def test_none_stays_none(self) -> None:
        """`exif_data is None` is what the upload task branches on."""
        image = baker.make(Image, image=None, exif_data=None)

        image.refresh_from_db()

        self.assertIsNone(image.exif_data)
        self.assertIsNone(_raw_column(image.pk))

    def test_nested_and_non_ascii_values_survive(self) -> None:
        payload = {"Artist": "Jokūbas", "Nested": {"a": [1, 2, {"b": None}]}, "Rational": 1.5}
        image = baker.make(Image, image=None, exif_data=payload)

        image.refresh_from_db()

        self.assertEqual(image.exif_data, payload)


class UndecryptableExifDataTests(TestCase):
    """What happens when no configured key matches - the fail_soft contract."""

    def _row_with_bad_ciphertext(self) -> Image:
        image = baker.make(Image, image=None, exif_data=_SAMPLE)
        # A well-formed Fernet token from a key this install does not have.
        bogus = "gAAAAABneverdecryptsBUTlooksLIKEaTOKEN=="
        with connection.cursor() as cursor:
            cursor.execute("UPDATE dashboard_images SET exif_data = %s WHERE id = %s", [bogus, image.pk])
        return image

    def test_it_reads_as_empty_rather_than_raising(self) -> None:
        """An Image row loads on every gallery page; one bad key must not 500 them."""
        image = self._row_with_bad_ciphertext()

        image.refresh_from_db()

        self.assertFalse(image.exif_data)
        self.assertEqual(image.exif_data.get("Make"), None)

    def test_saving_the_row_does_not_destroy_the_ciphertext(self) -> None:
        """The gap UndecryptableValue leaves for nullable fields, closed for this one.

        Without it, any save for an unrelated reason overwrites the still-
        recoverable ciphertext with the degraded default - and for this column
        there is no file left to re-extract it from.
        """
        image = self._row_with_bad_ciphertext()
        image.refresh_from_db()
        before = _raw_column(image.pk)
        self.assertIsInstance(image.exif_data, UndecryptableJSON)

        image.caption = "saved for an unrelated reason"
        image.save()

        self.assertEqual(_raw_column(image.pk), before, "an ordinary save destroyed the recoverable ciphertext")
