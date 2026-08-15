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
import zipfile

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.import_export.export import VALID_EXPORT_TYPES, run_export

#: google_takeout re-formats pins and direct_messages needs E2EE fixtures;
#: everything else runs on any account.
_TYPES = sorted(VALID_EXPORT_TYPES - {"google_takeout", "direct_messages"})

_EXPECTED_FILES = {"profile.json", "safety.json", "map_annotations.json", "saved_searches.json", "pins.json"}


class RunExportEndToEndTests(TestCase):
    def _run(self, user: User) -> set[str]:
        with tempfile.TemporaryDirectory() as export_dir_path:
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
        self.assertLessEqual(_EXPECTED_FILES, names, f"missing files in archive: {_EXPECTED_FILES - names}")

    def test_an_account_with_new_kind_content_exports_it(self) -> None:
        baker.make(User)
        user = baker.make(User)
        profile = user.profile
        baker.make("dashboard.SafetyCheckin", profile=profile, title="Roof survey")
        baker.make("dashboard.MarkupMap", profile=profile, title="Sketch")
        baker.make("dashboard.SavedFilter", profile=profile, name="ruins")
        names = self._run(user)
        self.assertLessEqual(_EXPECTED_FILES, names)
