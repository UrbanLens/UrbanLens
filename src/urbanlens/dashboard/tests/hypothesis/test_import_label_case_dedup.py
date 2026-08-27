"""Importing a label whose name differs only by case must skip, not fail.

`_import_labels` deduplicates against what the profile already has. It matched on
`name=` exactly, which was harmless while `Label` had no uniqueness: a differently
-cased duplicate just became a second row.

Since migration 0043 that is a constraint violation, so the exact-match lookup
misses "Abandoned" while importing "abandoned", falls through to
`Label.objects.create`, and raises `IntegrityError` - failing the **entire
import**, not just that row. A user re-importing their own export after renaming
a label's capitalisation would lose the whole restore.

The lookups are `name__iexact` now, matching the constraint they have to respect.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.import_export.import_data import ImportResult, _import_labels


class ImportLabelCaseDedupTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))

    def _run_import(self, rows: list[dict]) -> ImportResult:
        """Write the labels.json the importer reads, and import it.

        JSON, not CSV: ``_import_labels`` calls ``_read_json(data_dir,
        "labels.json")`` and returns immediately when that is empty - a CSV named
        labels.csv is simply not seen, so every assertion silently passes on an
        import that never ran.
        """
        result = ImportResult()
        with tempfile.TemporaryDirectory() as data_dir:
            (pathlib.Path(data_dir) / "labels.json").write_text(json.dumps(rows), encoding="utf-8")
            _import_labels(self.profile, data_dir, result, pin_uuid_map={}, label_uuid_map={})
        return result

    def _row(self, name: str) -> dict:
        from uuid import uuid4

        return {
            "uuid": str(uuid4()),
            "name": name,
            "kind": KIND_TAG,
            "description": "",
            "color": "",
            "icon": "",
            "order": 0,
            "is_user_label": True,
        }

    def test_a_case_differing_duplicate_is_skipped_not_created(self) -> None:
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)

        self._run_import([self._row("zzaudit abandoned")])

        self.assertEqual(Label.objects.filter(profile=self.profile, name__iexact="ZzAudit Abandoned").count(), 1)

    def test_the_import_does_not_fail_on_the_duplicate(self) -> None:
        """The regression that matters: an IntegrityError here aborts everything
        after it, so one re-cased label would cost the user the whole import."""
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)

        result = self._run_import([self._row("zzaudit abandoned"), self._row("ZzAudit Rooftop")])

        self.assertTrue(
            Label.objects.filter(profile=self.profile, name__iexact="ZzAudit Rooftop").exists(),
            "the row after the duplicate was never imported - the duplicate aborted the run",
        )
        self.assertEqual(result.created.get("labels", 0), 1)

    def test_an_exact_duplicate_is_still_skipped(self) -> None:
        Label.objects.create(profile=self.profile, name="ZzAudit Abandoned", kind=KIND_TAG)

        self._run_import([self._row("ZzAudit Abandoned")])

        self.assertEqual(Label.objects.filter(profile=self.profile, name__iexact="ZzAudit Abandoned").count(), 1)

    def test_a_genuinely_new_label_is_still_created(self) -> None:
        """Guards the checks above from passing because nothing is ever imported."""
        self._run_import([self._row("ZzAudit Brand New")])

        self.assertTrue(Label.objects.filter(profile=self.profile, name="ZzAudit Brand New").exists())
