"""run_export end-to-end: every export type produces its file and the zip assembles.

The per-type exporters have unit tests; nothing exercised the integration
path (`run_export` -> exporter dispatch -> `_build_zip`) at all, so a type
registered in `VALID_EXPORT_TYPES` but missing from the dispatch dict - or an
exporter raising on an empty account - would only surface for a real user.
This runs the full flow twice: an empty account (every exporter must tolerate
having nothing to export) and one with a row behind each of the chunk-469
additions.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import patch
import zipfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.import_export.export import VALID_EXPORT_TYPES, run_export

#: google_takeout re-formats pins and direct_messages needs E2EE fixtures;
#: everything else runs on any account.
_TYPES = sorted(VALID_EXPORT_TYPES - {"google_takeout", "direct_messages"})

#: The archive must carry the legacy areas plus every registered one. Registered
#: filenames are read from their declarations rather than repeated here - the
#: 2026-08-17 merge renamed two of them (safety.json -> safety_checkins.json,
#: saved_searches.json -> saved_filters.json) and a hardcoded list simply went
#: stale without saying why.
def _expected_files() -> set[str]:
    from urbanlens.dashboard.services.import_export.export import _REGISTERED_EXPORT_TYPES

    return {"profile.json", "pins.json"} | {export_type.filename for export_type in _REGISTERED_EXPORT_TYPES}


class RunExportEndToEndTests(TestCase):
    def _run(self, user: User) -> set[str]:
        # run_export schedules cleanup of export_dir_path via a Celery countdown
        # (services.import_export.export.schedule_export_cleanup) once the archive
        # is built. In UL_CELERY_TASK_ALWAYS_EAGER=True test runs that countdown is
        # not honored - eager mode runs the task inline, immediately - so the
        # cleanup would delete export.zip before this helper gets to read it.
        # Stub the enqueue per tests/CLAUDE.md's convention for background work.
        with (
            tempfile.TemporaryDirectory() as export_dir_path,
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
        ):
            ok = run_export(user.pk, _TYPES, export_dir_path, "https://example.test", job_id="test-job")
            self.assertTrue(ok, "run_export reported failure")
            zip_path = Path(export_dir_path) / "export.zip"
            self.assertTrue(zip_path.exists(), "no archive was built")
            with zipfile.ZipFile(zip_path) as zf:
                return {Path(name).name for name in zf.namelist()}

    def test_an_empty_account_exports_cleanly(self) -> None:
        baker.make(User)
        user = baker.make(User)
        names = self._run(user)
        self.assertLessEqual(_expected_files(), names, f"missing files in archive: {_expected_files() - names}")

    def test_an_account_with_new_kind_content_exports_it(self) -> None:
        baker.make(User)
        user = baker.make(User)
        profile = user.profile
        baker.make("dashboard.SafetyCheckin", profile=profile, title="Roof survey")
        baker.make("dashboard.MarkupMap", profile=profile, title="Sketch")
        baker.make("dashboard.SavedFilter", profile=profile, name="ruins")
        names = self._run(user)
        self.assertLessEqual(_expected_files(), names)
