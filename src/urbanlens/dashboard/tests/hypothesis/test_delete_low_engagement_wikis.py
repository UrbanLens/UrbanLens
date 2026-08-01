"""Tests for the delete_low_engagement_wikis management command.

Locks in the two independent deletion criteria - <=2 distinct pin owners, or
no active user edit - and the edge cases that make each nontrivial: pin
ownership is deduped per profile (not per pin), and "user edit" excludes both
reverted edits and null-editor (seed/system) edits, mirroring the precedent
in ``services.safety.destination_wiki_activity``. Also locks in that
deletion cascades to child wikis regardless of the child's own engagement,
since that's an easy thing to get wrong silently.
"""

from __future__ import annotations

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit


def _wiki(**kwargs) -> Wiki:
    location = baker.make(Location)
    return baker.make(Wiki, location=location, **kwargs)


def _add_pin_owners(wiki: Wiki, count: int) -> None:
    """Attach `count` pins, each owned by a distinct auto-generated profile."""
    for _ in range(count):
        baker.make(Pin, location=wiki.location, wiki=wiki)


def _add_user_edit(wiki: Wiki, **kwargs) -> WikiEdit:
    editor = Profile.objects.get(user=baker.make(User))
    return baker.make(WikiEdit, wiki=wiki, editor=editor, changes={"name": {"from": "a", "to": "b"}}, **kwargs)


def _run_command(*args) -> str:
    out = StringIO()
    call_command("delete_low_engagement_wikis", *args, stdout=out)
    return out.getvalue()


class DeleteLowEngagementWikisTests(TestCase):
    """Wikis matching either deletion criterion are reported, then deleted only with --yes."""

    def test_dry_run_by_default_reports_but_does_not_delete(self) -> None:
        wiki = _wiki()  # 0 pin owners, 0 edits - qualifies on both counts
        output = _run_command()
        self.assertIn(f"pk={wiki.pk}", output)
        self.assertIn("Dry run", output)
        self.assertTrue(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_yes_flag_deletes_matching_wikis(self) -> None:
        wiki = _wiki()
        output = _run_command("--yes")
        self.assertIn("Deleted 1 wiki", output)
        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_no_matches_reports_and_deletes_nothing(self) -> None:
        wiki = _wiki()
        _add_pin_owners(wiki, 3)
        _add_user_edit(wiki)
        output = _run_command("--yes")
        self.assertIn("No wikis matched", output)
        self.assertTrue(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_wiki_kept_with_enough_pin_owners_and_a_user_edit(self) -> None:
        wiki = _wiki()
        _add_pin_owners(wiki, 3)
        _add_user_edit(wiki)
        _run_command("--yes")
        self.assertTrue(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_wiki_deleted_at_exactly_two_pin_owners_even_with_a_user_edit(self) -> None:
        wiki = _wiki()
        _add_pin_owners(wiki, 2)
        _add_user_edit(wiki)
        _run_command("--yes")
        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_wiki_deleted_with_enough_pins_but_zero_edits(self) -> None:
        wiki = _wiki()
        _add_pin_owners(wiki, 5)
        _run_command("--yes")
        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_reverted_edits_do_not_count_as_user_edits(self) -> None:
        wiki = _wiki()
        _add_pin_owners(wiki, 5)
        _add_user_edit(wiki, reverted=True)
        _run_command("--yes")
        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_null_editor_seed_edits_do_not_count_as_user_edits(self) -> None:
        wiki = _wiki()
        _add_pin_owners(wiki, 5)
        baker.make(WikiEdit, wiki=wiki, editor=None, changes={"name": {"from": "a", "to": "b"}})
        _run_command("--yes")
        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())

    def test_annotation_counts_are_accurate_across_both_joined_relations(self) -> None:
        """Regression guard: combining two Count(distinct=True) annotations
        from separate reverse relations (pins, edits) in one query risks a
        join fan-out inflating the counts. Give the wiki enough pin owners to
        clear that threshold on its own, but zero *active* user edits (one
        reverted, one null-editor) so it still matches - via the edit
        criterion - and gets its counts printed. If the pins join inflated
        user_edit_count, or vice versa, the printed numbers would be wrong
        even though the keep/delete outcome could still look right."""
        wiki = _wiki()
        _add_pin_owners(wiki, 4)
        _add_user_edit(wiki, reverted=True)
        baker.make(WikiEdit, wiki=wiki, editor=None, changes={"name": {"from": "a", "to": "b"}})

        output = _run_command()  # dry run

        self.assertIn(f"[pk={wiki.pk}] {wiki.name!r} pin_owners=4 user_edits=0", output)

    def test_deleting_a_qualifying_parent_cascades_to_its_child_wikis(self) -> None:
        parent = _wiki()  # 0 pin owners, 0 edits - qualifies
        child = baker.make(Wiki, location=baker.make(Location), parent_wiki=parent)
        _add_pin_owners(child, 10)
        _add_user_edit(child)  # child would NOT qualify on its own

        output = _run_command("--yes")

        self.assertIn("child wiki", output.lower())
        self.assertFalse(Wiki.objects.filter(pk=parent.pk).exists())
        self.assertFalse(Wiki.objects.filter(pk=child.pk).exists())

    def test_pin_owner_count_dedupes_multiple_pins_from_the_same_profile(self) -> None:
        # A profile can only have one pin per Location (db_pin_unique_location_per_profile),
        # so exercising the dedup means each of 2 profiles pinning two
        # *different* locations, all explicitly linked to the same wiki: 4
        # pins total, but only 2 distinct owners. If pin_owner_count counted
        # raw pins instead of distinct profiles it would read 4 (above the
        # threshold) and wrongly survive; deduped correctly it's 2 and this
        # wiki should be deleted.
        wiki = _wiki()
        for _ in range(2):
            owner = Profile.objects.get(user=baker.make(User))
            baker.make(Pin, location=baker.make(Location), wiki=wiki, profile=owner)
            baker.make(Pin, location=baker.make(Location), wiki=wiki, profile=owner)

        _run_command("--yes")

        self.assertFalse(Wiki.objects.filter(pk=wiki.pk).exists())
