"""Import category creation matches on kind, not name alone.

PROBLEMS 2026-08-13: both Google Maps import paths looked up the list's
category label with ``kind`` in ``defaults`` only, so the get half matched
across every kind - a user with a *tag* named like the imported list got that
tag used as the category, and no category was ever created. ``kind`` now
lives in the lookup, matching the pattern the other label-creating sites
already used.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label


class ImportCategoryLabelKindTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.tag = baker.make(Label, profile=self.profile, kind=KIND_TAG, name="Factory")

    def _resolve_category(self, stem: str) -> Label:
        """The exact get_or_create both import sites now perform."""
        label, _created = Label.objects.get_or_create(
            profile=self.profile,
            name__iexact=stem,
            kind=KIND_CATEGORY,
            defaults={"name": stem},
        )
        return label

    def test_a_same_named_tag_is_not_mistaken_for_the_category(self) -> None:
        category = self._resolve_category("Factory")
        self.assertNotEqual(category.pk, self.tag.pk, "the import must not file pins under an unrelated same-named tag")
        self.assertEqual(category.kind, KIND_CATEGORY)
        self.tag.refresh_from_db()
        self.assertEqual(self.tag.kind, KIND_TAG, "the existing tag must be left untouched")

    def test_an_existing_category_is_reused_case_insensitively(self) -> None:
        # New profiles are seeded with default labels including a "Factory"
        # category (which is also why setUp's tag "Factory" coexists with it) -
        # the lookup must reuse that row regardless of the imported stem's case.
        existing = Label.objects.get(profile=self.profile, kind=KIND_CATEGORY, name__iexact="factory")
        before = Label.objects.filter(profile=self.profile, kind=KIND_CATEGORY).count()
        category = self._resolve_category("FACTORY")
        self.assertEqual(category.pk, existing.pk)
        self.assertEqual(Label.objects.filter(profile=self.profile, kind=KIND_CATEGORY).count(), before)
