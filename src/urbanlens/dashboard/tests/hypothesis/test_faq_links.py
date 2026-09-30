"""Links into the FAQ land on an answer that exists, and its section tabs name sections that exist."""

from __future__ import annotations

from pathlib import Path
import re

from django.urls import reverse

from urbanlens.core.tests.testcase import TestCase

_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
_FAQ_LINK = re.compile(r"""\{%\s*url\s+['"]faq['"]\s*%\}#([\w-]+)""")


class FaqLinkTests(TestCase):
    def setUp(self) -> None:
        self.html = self.client.get(reverse("faq")).content.decode()

    def _ids(self) -> set[str]:
        return set(re.findall(r'\bid="([^"]+)"', self.html))

    def test_every_deep_link_names_an_answer_on_the_page(self) -> None:
        fragments = {
            (path.relative_to(_TEMPLATES).as_posix(), fragment)
            for path in _TEMPLATES.rglob("*.html")
            for fragment in _FAQ_LINK.findall(path.read_text(encoding="utf-8"))
        }
        self.assertGreaterEqual(len(fragments), 4)
        ids = self._ids()
        self.assertEqual([link for link in sorted(fragments) if link[1] not in ids], [])

    def test_the_section_tabs_point_at_sections_on_the_page(self) -> None:
        tabs = re.findall(r'<a href="#([\w-]+)"[^>]*\bdata-section-tab\b', self.html)
        self.assertGreaterEqual(len(tabs), 4)
        ids = self._ids()
        self.assertEqual([tab for tab in tabs if tab not in ids], [])
