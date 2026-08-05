"""Delete community wikis nobody is actually using.

A ``Wiki`` is auto-created (as an unofficial draft) for a Location well before
anyone asks for one, so background enrichment has a head start - see
``dashboard.tasks.enrich_wiki_location``. Most of those never attract a
community: no one else pins the place, and no one ever edits the page. This
command finds them and, with ``--yes``, deletes them.

Two independent criteria, either of which qualifies a wiki for deletion:

* **Too few pin owners** - at most :data:`MIN_PIN_OWNERS` distinct *profiles*
  hold a pin linked to the wiki. Counted per profile rather than per pin,
  because one person pinning several locations onto a wiki is still one
  person's interest, not a community's.
* **No active user edit** - nobody has edited the page in a way that still
  stands. A reverted edit didn't survive, and a null-editor edit is a
  seed/system write rather than a person, so neither counts. This mirrors how
  every other engagement signal in the project reads edit history (see
  ``WikiEditQuerySet.active`` and ``services.achievements.metrics``).

Dry run by default: deleting community content is not something to do as a
side effect of running a report, so the destructive half is behind an explicit
``--yes``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from urbanlens.dashboard.models.wiki.model import Wiki

#: A wiki with at most this many distinct pin owners is not a community page.
#: Inclusive - a wiki sitting exactly on the threshold qualifies for deletion,
#: since two people is a coincidence rather than a community.
MIN_PIN_OWNERS = 2


class Command(BaseCommand):
    """Report (and optionally delete) wikis with no real community engagement."""

    help = "Delete community wikis with too few pin owners or no surviving user edits. Dry run unless --yes is passed."

    def add_arguments(self, parser) -> None:
        """Register this command's arguments."""
        parser.add_argument("--yes", action="store_true", help="Actually delete. Without this the command only reports.")

    def handle(self, *args, **options) -> None:
        """Find matching wikis, print them, and delete them when ``--yes`` was passed."""
        matches = list(self._matching_wikis())
        if not matches:
            self.stdout.write("No wikis matched the deletion criteria.")
            return

        pks = [wiki.pk for wiki in matches]
        for wiki in matches:
            self.stdout.write(f"[pk={wiki.pk}] {wiki.name!r} pin_owners={wiki.pin_owner_count} user_edits={wiki.user_edit_count}")

        # Reported separately because a child is deleted for its parent's lack
        # of engagement, not its own - a busy child wiki disappearing is the
        # surprising part of this command, so it should never be silent.
        cascaded = Wiki.objects.filter(parent_wiki__in=pks).exclude(pk__in=pks).count()
        if cascaded:
            self.stdout.write(f"...plus {cascaded} child wiki(s), deleted with their parent regardless of their own engagement.")

        if not options["yes"]:
            self.stdout.write(f"Dry run: {len(matches)} wiki(s) would be deleted. Re-run with --yes to delete them.")
            return

        deleted, _ = Wiki.objects.filter(pk__in=pks).delete()
        self.stdout.write(f"Deleted {len(matches)} wiki(s) ({deleted} rows including cascades).")

    @staticmethod
    def _matching_wikis():
        """Wikis qualifying for deletion, annotated with the counts that decided it.

        Both counts are ``distinct=True`` because they annotate across two
        separate reverse relations in one query: joining ``pins`` and ``edits``
        together fans out to their cross product, which would otherwise
        multiply each count by the other relation's row count.

        Returns:
            A ``Wiki`` queryset annotated with ``pin_owner_count`` and
            ``user_edit_count``.
        """
        return (
            Wiki.objects.annotate(
                pin_owner_count=Count("pins__profile", distinct=True),
                user_edit_count=Count("edits", distinct=True, filter=Q(edits__reverted=False, edits__editor__isnull=False)),
            )
            .filter(Q(pin_owner_count__lte=MIN_PIN_OWNERS) | Q(user_edit_count=0))
            .order_by("pk")
        )
