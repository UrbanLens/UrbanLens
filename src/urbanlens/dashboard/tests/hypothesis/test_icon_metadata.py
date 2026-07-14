"""Consistency tests for the icon picker's emoji metadata.

``ICON_KEYWORDS`` supplies the extra search terms rendered into each icon
button's ``data-keywords`` attribute; a keyword entry whose emoji key is not
actually offered by ``ICON_CATEGORIES`` can never match anything, so these
tests keep the two structures in sync.
"""

from __future__ import annotations

from unittest import TestCase

from urbanlens.dashboard.models.labels.meta import ICON_CATEGORIES, ICON_KEYWORDS
from urbanlens.dashboard.templatetags.dashboard_tags import icon_keywords


def _all_picker_icons() -> set[str]:
    """Every emoji character offered by the icon picker, across all categories."""
    return {icon for _label, pairs in ICON_CATEGORIES.values() for icon, _ in pairs}


class IconKeywordConsistencyTests(TestCase):
    """Every keyword entry must belong to a real picker icon (and vice versa stay useful)."""

    def test_every_keyword_key_is_a_picker_icon(self) -> None:
        icons = _all_picker_icons()
        orphans = sorted(k for k in ICON_KEYWORDS if k not in icons)
        self.assertEqual(orphans, [], f"ICON_KEYWORDS entries for emojis missing from ICON_CATEGORIES: {[ascii(o) for o in orphans]}")

    def test_keywords_are_lowercase_space_separated(self) -> None:
        for icon, keywords in ICON_KEYWORDS.items():
            self.assertEqual(keywords, keywords.lower(), f"keywords for {ascii(icon)} must be lowercase")
            self.assertNotIn("\n", keywords)

    def test_icon_keywords_filter_returns_registered_terms(self) -> None:
        self.assertIn("derelict", icon_keywords("🏚️"))

    def test_icon_keywords_filter_handles_unknown_values(self) -> None:
        self.assertEqual(icon_keywords("not-an-emoji"), "")
        self.assertEqual(icon_keywords(None), "")
