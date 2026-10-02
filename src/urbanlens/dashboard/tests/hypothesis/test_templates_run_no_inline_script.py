"""No template carries a program or an ``on*=`` handler, so the CSP can drop ``'unsafe-inline'`` (P34).

Behaviour is named in markup (``data-*`` attributes, JSON islands) and lives in the bundles. A JSON island is data, so
it may stay inline.
"""

from __future__ import annotations

from pathlib import Path

from django.template.loader import render_to_string

from urbanlens.core.tests.inline_scripts import executable_blocks, inline_handlers
from urbanlens.core.tests.testcase import SimpleTestCase

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"


def _offenders() -> dict[str, list[str]]:
    found = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = path.read_bytes()
        names = [f"<script> {block[:40]!r}" for block in executable_blocks(source)] + inline_handlers(source)
        if names:
            found[str(path.relative_to(TEMPLATES))] = names
    return found


class TemplatesRunNoInlineScriptTests(SimpleTestCase):
    def test_templates_were_found(self) -> None:
        self.assertGreater(len(list(TEMPLATES.rglob("*.html"))), 400)

    def test_no_template_carries_a_script_or_a_handler(self) -> None:
        self.assertEqual(_offenders(), {})

    def test_a_data_attribute_ending_in_on_is_not_a_handler(self) -> None:
        self.assertEqual(inline_handlers(b'<div data-mode-only="photos" data-show-onboarding="1">'), [])
        self.assertEqual(inline_handlers(b'<button onclick="go()">'), ["onclick"])


class ImportProgressFragmentTests(SimpleTestCase):
    def test_a_finished_import_marks_the_map_pins_dirty_through_markup(self) -> None:
        html = render_to_string(
            "dashboard/partials/tools/import_progress.html",
            {"status": "done", "job_id": "4f6c2a52-0b8e-4b0e-9d5f-1f2a3b4c5d6e", "result": {}},
        ).encode()
        self.assertIn(b"data-pins-dirty", html)
        self.assertEqual(executable_blocks(html), [])

    def test_a_running_import_does_not(self) -> None:
        html = render_to_string(
            "dashboard/partials/tools/import_progress.html",
            {"status": "running", "job_id": "4f6c2a52-0b8e-4b0e-9d5f-1f2a3b4c5d6e", "progress": 5},
        ).encode()
        self.assertNotIn(b"data-pins-dirty", html)
