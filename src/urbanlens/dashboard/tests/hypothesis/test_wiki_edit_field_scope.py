"""A wiki edit must write only the fields it edited."""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit, revert_wiki_edit


class WikiEditFieldScopeTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.editor = baker.make(User).profile
        self.other = baker.make(User).profile
        self.location = baker.make("dashboard.Location", latitude=41.2, longitude=-73.9)
        self.wiki = baker.make(
            "dashboard.Wiki", location=self.location, name="Mill", description="Original description"
        )

    def _snapshot(self) -> Wiki:
        """One editor's request-scoped copy of the row."""
        return Wiki.objects.get(pk=self.wiki.pk)

    def test_editing_one_field_does_not_revert_a_concurrent_edit_to_another(self) -> None:
        stale = self._snapshot()

        apply_wiki_edit(self._snapshot(), self.other, {"description": "Someone else's research"})
        apply_wiki_edit(stale, self.editor, {"name": "Mill Complex"})

        self.wiki.refresh_from_db()
        self.assertEqual(
            self.wiki.description, "Someone else's research", "a concurrent edit to another field was reverted"
        )
        self.assertEqual(self.wiki.name, "Mill Complex", "the edit that was actually made did not land")

    def test_an_edit_does_not_reset_a_field_written_by_another_subsystem(self) -> None:
        """The gallery sets ``cover_photo`` through its own targeted update; editing must not undo it."""
        stale = self._snapshot()
        photo = baker.make("dashboard.Image", wiki=self.wiki)
        Wiki.objects.filter(pk=self.wiki.pk).update(cover_photo=photo)

        apply_wiki_edit(stale, self.editor, {"name": "Mill Complex"})

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.cover_photo_id, photo.pk, "an edit reset a field owned by a different writer")

    def test_a_revert_does_not_clobber_a_field_it_deliberately_skipped(self) -> None:
        """The complement to ``revert_edit_fields``' own conflict check.

        That check leaves a field alone when someone changed it since."""
        target = apply_wiki_edit(self._snapshot(), self.editor, {"name": "Mill Complex"})
        stale = self._snapshot()

        apply_wiki_edit(self._snapshot(), self.other, {"description": "Later research"})
        revert_wiki_edit(self.location, stale, self.editor, target)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.description, "Later research", "the revert clobbered a field it never touched")
        self.assertEqual(self.wiki.name, "Mill", "the revert did not restore the field it targeted")

    def test_an_edit_still_writes_every_field_it_was_given(self) -> None:
        """Narrowing the write must not narrow it to nothing."""
        wiki = self._snapshot()

        apply_wiki_edit(
            wiki,
            self.editor,
            {"name": "Mill Complex", "description": "Rewritten", "date_abandoned": "1974-03-02"},
        )

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Mill Complex")
        self.assertEqual(self.wiki.description, "Rewritten")
        self.assertEqual(str(self.wiki.date_abandoned), "1974-03-02")
