"""A wiki edit must write only the fields it edited.

``apply_wiki_edit`` collects the submitted fields into ``new_vals``, sets them
on the wiki, and then calls a bare ``save()`` - which writes *every* column from
that instance, not just the edited ones. The instance was loaded when the
request started, so a whole-row write reverts anything committed in between.

A wiki is the worst possible model for this. It is community-editable by
design: concurrent editors are the normal case, not the pathological one, and
two people editing different fields of one wiki is exactly what the feature
invites. The row also has writers that are not edits at all - viewing a wiki
marks ``viewed_by_other`` through a targeted ``.update()``, and the naming and
consensus services write their own columns.

The service already knows this hazard exists: ``revert_edit_fields`` checks each
field's current value against the edit's recorded "to" value and refuses to
restore a field someone changed since, precisely so a revert "would [not]
silently clobber that later change". The bare ``save()`` two lines later
clobbers it anyway, through every field the revert did *not* touch.

Both call sites are covered here. The interleaving is modelled with two
snapshots of one row rather than threads: two instances loaded from the same
wiki are two concurrent editors' request state, and driving them in sequence
reproduces the write-after-stale-read that concurrency produces.
"""

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
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Mill", description="Original description")

    def _snapshot(self) -> Wiki:
        """One editor's request-scoped copy of the row."""
        return Wiki.objects.get(pk=self.wiki.pk)

    def test_editing_one_field_does_not_revert_a_concurrent_edit_to_another(self) -> None:
        stale = self._snapshot()

        apply_wiki_edit(self._snapshot(), self.other, {"description": "Someone else's research"}, strict=True)
        apply_wiki_edit(stale, self.editor, {"name": "Mill Complex"}, strict=True)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.description, "Someone else's research", "a concurrent edit to another field was reverted")
        self.assertEqual(self.wiki.name, "Mill Complex", "the edit that was actually made did not land")

    def test_an_edit_does_not_reset_a_flag_written_by_another_subsystem(self) -> None:
        """Viewing a wiki sets ``viewed_by_other`` via a targeted update; editing must not undo it."""
        stale = self._snapshot()
        Wiki.objects.filter(pk=self.wiki.pk).update(viewed_by_other=True)

        apply_wiki_edit(stale, self.editor, {"name": "Mill Complex"}, strict=True)

        self.wiki.refresh_from_db()
        self.assertTrue(self.wiki.viewed_by_other, "an edit reset a flag owned by a different writer")

    def test_a_revert_does_not_clobber_a_field_it_deliberately_skipped(self) -> None:
        """The complement to ``revert_edit_fields``' own conflict check.

        That check leaves a field alone when someone changed it since. It is
        defeated if the save then writes the whole row from a snapshot that
        predates the change.
        """
        target = apply_wiki_edit(self._snapshot(), self.editor, {"name": "Mill Complex"}, strict=True)
        stale = self._snapshot()

        apply_wiki_edit(self._snapshot(), self.other, {"description": "Later research"}, strict=True)
        revert_wiki_edit(self.location, stale, self.editor, target)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.description, "Later research", "the revert clobbered a field it never touched")
        self.assertEqual(self.wiki.name, "Mill", "the revert did not restore the field it targeted")

    def test_an_edit_still_writes_every_field_it_was_given(self) -> None:
        """Narrowing the write must not narrow it to nothing."""
        wiki = self._snapshot()

        apply_wiki_edit(wiki, self.editor, {"name": "Mill Complex", "description": "Rewritten", "date_abandoned": "1974-03-02"}, strict=True)

        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.name, "Mill Complex")
        self.assertEqual(self.wiki.description, "Rewritten")
        self.assertEqual(str(self.wiki.date_abandoned), "1974-03-02")
