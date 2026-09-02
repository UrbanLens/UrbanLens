"""Tests for services.ai.page_help (plan §10, batch 4).

The contract test parses themes/header.html's own primary-nav ``{% url %}``
tags - the same template-parsing approach hotkeys.contract.test.ts already
uses for Settings > Shortcuts - so a new nav link with no PAGE_HELP entry
fails the build instead of quietly leaving the assistant unable to explain
that page.
"""

from __future__ import annotations

from pathlib import Path
import re

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.ai.page_help import PAGE_HELP, get_page_help

_HEADER_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "dashboard" / "partials" / "layout" / "header.html"
#: Rendered only for an anonymous visitor - not part of the signed-in
#: primary nav the assistant's own audience (logged-in users) actually uses.
_ANONYMOUS_ONLY = {"about", "values", "faq"}


class GetPageHelpTests(SimpleTestCase):
    def test_a_known_url_name_returns_its_help(self) -> None:
        help_ = get_page_help("map.view")
        self.assertIsNotNone(help_)
        assert help_ is not None
        self.assertEqual(help_.title, "Map")
        self.assertTrue(help_.key_actions)

    def test_an_unknown_url_name_returns_none(self) -> None:
        self.assertIsNone(get_page_help("not.a.real.page"))

    def test_an_empty_string_returns_none(self) -> None:
        self.assertIsNone(get_page_help(""))


class PageHelpContractTests(SimpleTestCase):
    """Every primary-nav page has PAGE_HELP - parsed straight from header.html, not hand-copied."""

    def test_the_template_is_where_we_think_it_is(self) -> None:
        self.assertTrue(_HEADER_TEMPLATE.is_file())

    def _primary_nav_url_names(self) -> set[str]:
        template = _HEADER_TEMPLATE.read_text(encoding="utf-8")
        url_names: set[str] = set()
        for class_name in ("app-nav-links", "app-nav-drawer-links"):
            block = re.search(rf'<ul class="{class_name}">(.*?)</ul>', template, re.DOTALL)
            assert block is not None, f"couldn't find <ul class=\"{class_name}\"> in {_HEADER_TEMPLATE}"
            url_names.update(re.findall(r"\{% url '([\w.]+)' %\}", block.group(1)))
        return url_names - _ANONYMOUS_ONLY

    def test_every_primary_nav_url_name_has_page_help(self) -> None:
        url_names = self._primary_nav_url_names()
        self.assertTrue(url_names, "parsed no url_names at all - the parsing regex likely drifted from header.html")
        for url_name in url_names:
            self.assertIn(url_name, PAGE_HELP, f"{url_name!r} is in the primary nav but has no PAGE_HELP entry")

    def test_every_page_help_entry_has_a_title_and_at_least_one_action(self) -> None:
        for url_name, help_ in PAGE_HELP.items():
            self.assertTrue(help_.title, f"{url_name!r} has a blank title")
            self.assertTrue(help_.key_actions, f"{url_name!r} has no key_actions")
