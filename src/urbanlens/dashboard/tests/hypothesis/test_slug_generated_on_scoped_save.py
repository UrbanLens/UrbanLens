"""A slug generated while saving a slugless row is persisted even when the save names its fields (P5)."""

from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location


class ScopedSaveSlugTests(TestCase):
    def test_a_scoped_save_of_a_slugless_row_stores_the_slug_it_generates(self) -> None:
        location = Location.objects.create(latitude=40.0, longitude=-74.0)
        Location.objects.filter(pk=location.pk).update(slug=None)
        location.refresh_from_db()

        location.save(update_fields=["updated"])

        self.assertIsNotNone(location.slug, "no slug was generated, so the rest proves nothing")
        self.assertEqual(Location.objects.get(pk=location.pk).slug, location.slug)
