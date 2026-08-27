"""Reverting a revert un-marks the original edit.

A revert is recorded as a new WikiEdit carrying the inverted diff, with the
target flagged ``reverted``. Reverting that revert puts the original content
back in force - so the original's flag (which the history display and the
wiki-edits achievement metric both read) must clear, or the log says an edit
is dead while its content stands live. Cleared only on a full revert; a
partial one (conflicting later edits skipped fields) leaves the conservative
flag in place.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit, revert_wiki_edit


class RevertOfRevertTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.author = baker.make(User).profile
        self.reverter = baker.make(User).profile
        self.location = baker.make("dashboard.Location", latitude=41.2, longitude=-73.9)
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Mill")

    def test_a_full_revert_of_a_revert_unmarks_the_original(self) -> None:
        original = apply_wiki_edit(self.wiki, self.author, {"name": "New Mill Name"}, strict=True)
        assert original is not None

        revert_edit, skipped = revert_wiki_edit(self.location, self.wiki, self.reverter, original)
        assert revert_edit is not None and not skipped
        original.refresh_from_db()
        self.assertTrue(original.reverted)

        second_revert, skipped = revert_wiki_edit(self.location, self.wiki, self.author, revert_edit)
        assert second_revert is not None and not skipped

        original.refresh_from_db()
        self.assertFalse(original.reverted, "the original edit's content is back in force - its flag must not say otherwise")
        self.assertIsNone(original.reverted_by)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "New Mill Name")

    def test_reverting_an_ordinary_edit_does_not_touch_other_flags(self) -> None:
        first = apply_wiki_edit(self.wiki, self.author, {"name": "First"}, strict=True)
        assert first is not None
        revert_edit, _ = revert_wiki_edit(self.location, self.wiki, self.reverter, first)
        assert revert_edit is not None

        second = apply_wiki_edit(self.wiki, self.author, {"name": "Second"}, strict=True)
        assert second is not None
        revert_second, _ = revert_wiki_edit(self.location, self.wiki, self.reverter, second)
        assert revert_second is not None

        first.refresh_from_db()
        self.assertTrue(first.reverted, "reverting an unrelated edit must not resurrect earlier reverted ones")
