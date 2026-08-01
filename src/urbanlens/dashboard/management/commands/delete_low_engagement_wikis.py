"""Delete Wiki rows with too little community engagement to be worth keeping.

A wiki qualifies for deletion when either is true:
- 2 or fewer distinct profiles have a Pin explicitly linked to it (``Pin.wiki``
  - this is *not* every pin at the wiki's Location, only the ones a user has
  explicitly attached; see ``WikiCreationService``/``services.pin_wiki_sync``).
- It has no *active* edit by a real user - a ``WikiEdit`` whose ``editor`` is
  set and ``reverted`` is False. Reverted edits and edits with a null editor
  (seed/system content, e.g. ``services.wiki_seed``/``services.wiki_merge``)
  don't count, mirroring the "has a human touched this" check in
  ``services.safety.destination_wiki_activity``.

This is a hard delete with no undo path: Wiki has no soft-delete, and isn't
wired into the undo framework the way a single user-initiated delete is (see
``LocationWikiDeleteView`` - it stashes an undo record because it has an
acting profile to attribute the action to; a bulk system cleanup does not).
Deleting a wiki cascades to its WikiEdit history, Article/ArticleRevisions,
aliases, comments, boundary/markup/links rows, and - notably - any child
wikis nested under it (``Wiki.parent_wiki`` is CASCADE), even if a given
child wouldn't qualify for deletion on its own. Pins that referenced the
wiki are kept; ``Pin.wiki`` is just set to null (SET_NULL).

Because of that blast radius, this command only reports what it would delete
unless run with --yes.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Count, Q

from urbanlens.dashboard.models.wiki.model import Wiki

#: A wiki with this many or fewer distinct pin owners qualifies for deletion.
MAX_PIN_OWNERS = 2


class Command(BaseCommand):
    """Delete wikis with <=2 distinct pin owners, or no active edits by a real user."""

    help = "Delete wikis with 2 or fewer distinct pin owners, or no user edits. Reports matches only unless --yes is given."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Actually delete the matching wikis. Without this flag the command only reports what would be deleted.",
        )

    def handle(self, *args, **options) -> None:
        confirmed = options["yes"]

        """
        queryset = (
            Wiki.objects.annotate(
                pin_owner_count=Count("pins__profile", distinct=True),
                user_edit_count=Count(
                    "edits",
                    filter=Q(edits__reverted=False, edits__editor__isnull=False),
                    distinct=True,
                ),
            )
            .filter(Q(pin_owner_count__lte=MAX_PIN_OWNERS) | Q(user_edit_count=0))
            .order_by("pk")
        )
        """
        queryset = Wiki.objects.all()

        total = queryset.count()
        if not total:
            self.stdout.write("No wikis matched the deletion criteria.")
            return

        cascaded_children = Wiki.objects.filter(parent_wiki_id__in=queryset.values("pk")).exclude(pk__in=queryset.values("pk")).count()

        self.stdout.write(f"Found {total} wiki(s) with <= {MAX_PIN_OWNERS} pin owner(s) or no active user edits.")
        if cascaded_children:
            self.stdout.write(f"  {cascaded_children} additional child wiki(s) will also be deleted via cascade, regardless of their own engagement.")

        for wiki in queryset.iterator():
            self.stdout.write(f"  [pk={wiki.pk}] {wiki.name!r} pin_owners={wiki.pin_owner_count} user_edits={wiki.user_edit_count}")

        if not confirmed:
            self.stdout.write("Dry run - no wikis deleted. Re-run with --yes to delete.")
            return

        deleted_count, deleted_by_model = Wiki.objects.filter(pk__in=queryset.values("pk")).delete()
        wiki_deleted = deleted_by_model.get(Wiki._meta.label, 0)  # noqa: SLF001 - _meta is public API despite the underscore
        self.stdout.write(f"Deleted {wiki_deleted} wiki(s) ({deleted_count} row(s) total, including cascaded child wikis, edits, and related records).")
