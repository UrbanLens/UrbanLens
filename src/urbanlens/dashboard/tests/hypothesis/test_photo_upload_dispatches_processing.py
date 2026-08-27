"""Every caller of ``upload_photo_for_owner`` must enqueue EXIF processing.

``upload_photo_for_owner`` stores the file exactly as uploaded; GPS/EXIF
stripping, downscaling and conversion all live in
``tasks.process_image_upload``. A caller that forgets the dispatch leaves a
user's camera GPS in a stored, servable file - a privacy leak that no test
of the helper itself would catch, because the helper is behaving correctly.

Today there is exactly one caller (the gallery upload controller) and it
dispatches. This is the guard for the *next* one: a source-level check, in
the spirit of the bulk-write signal guard, because the failure mode is an
omission at a new call site rather than a wrong result at an existing one.
"""

from __future__ import annotations

import pathlib
import re

from urbanlens.core.tests.testcase import SimpleTestCase

_SRC = pathlib.Path(__file__).resolve().parents[3] / "dashboard"
_HELPER = "upload_photo_for_owner"
_DISPATCH = "process_image_upload"


def _callers() -> list[pathlib.Path]:
    """Every non-test module that calls the upload helper."""
    hits = []
    for path in _SRC.rglob("*.py"):
        if "tests" in path.parts or path.name == "uploads.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(rf"\b{_HELPER}\s*\(", text):
            hits.append(path)
    return hits


class PhotoUploadDispatchTests(SimpleTestCase):
    def test_the_helper_still_has_callers(self) -> None:
        """Anti-vacuity: if the helper is renamed or inlined, this suite must fail loudly rather than pass empty."""
        self.assertTrue(_callers(), f"no caller of {_HELPER}() found - was it renamed? This guard would silently pass forever.")

    def test_every_caller_enqueues_exif_processing(self) -> None:
        missing = [str(path.relative_to(_SRC)) for path in _callers() if _DISPATCH not in path.read_text(encoding="utf-8")]
        self.assertEqual(
            missing,
            [],
            f"these modules call {_HELPER}() without enqueueing {_DISPATCH} - uploaded photos would keep their GPS EXIF: {missing}",
        )
