"""One-off, read-only audit for BLOCKED ``Friendship`` rows that may predate
``services.social.friendship.block_profile``'s ``from_profile``/``to_profile``
normalization fix.

``block_profile`` used to reuse whichever row already joined a pair without
swapping the two foreign keys, so a block placed on an inbound friend request
(or an existing friend) could leave the row's ``from_profile`` - the only
record of who blocked whom, per ``_placed_the_block`` - pointing at the
person who was actually *blocked*, not the blocker. A block placed against a
complete stranger, with no prior row, was never affected: the fresh-row path
(``Friendship.objects.create(from_profile=actor, ...)``) always stamped the
actor correctly, before and after the fix. See docs/PROBLEMS.md, "Blocked
Friendship rows created before block_profile started normalizing direction
may record the wrong blocker".

There is no ``blocked_by`` column and never was, so nothing here can prove a
given row is actually inverted - that signal is gone. What this reports is
which BLOCKED rows are even *reachable* by the bug: created before the fix
shipped, and showing a sign of having started life as something other than a
fresh block (a stored ``request_message``, or an ``updated`` timestamp well
after ``created``) - i.e. the row went through the reuse path the bug lived
in, rather than the always-correct create path.

Never writes. Flagged rows are for a human to review - and, where the two
people involved can confirm who actually placed the block, correct by hand.
"""

from __future__ import annotations

import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_date

from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship

#: Below this gap between `created` and `updated`, a row is treated as having
#: gone straight from nonexistent to BLOCKED in one `Friendship.objects.create`
#: call - the always-correct path - rather than through the reuse path the bug
#: lived in. Generous on purpose: a fresh create's two timestamps come from the
#: same INSERT and should be identical (or microseconds apart), so minutes of
#: slack costs nothing while still catching same-day reuse.
_FRESH_CREATE_TOLERANCE = datetime.timedelta(minutes=5)


class Command(BaseCommand):
    """Report BLOCKED Friendship rows whose from_profile/to_profile direction is worth reviewing."""

    help = "Read-only audit of BLOCKED Friendship rows that may predate block_profile's direction-normalization fix. Never writes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--before",
            required=True,
            help=(
                "ISO date (YYYY-MM-DD), exclusive: only rows created before this are audited. "
                "Deliberately not defaulted - this repo's history only pins the fix to a squashed "
                "range (docs/PROBLEMS.md records the finding as noted 2026-07-26; the fix reached "
                "this repo's main branch in the 2026-07-30 release merge). Pass the date you know "
                "the fix actually reached your production database."
            ),
        )

    def handle(self, *args, **options):
        cutoff_date = parse_date(options["before"])
        if cutoff_date is None:
            raise CommandError(f"--before must be an ISO date (YYYY-MM-DD), got {options['before']!r}")

        candidates = (
            Friendship.objects.filter(status=FriendshipStatus.BLOCKED, created__date__lt=cutoff_date)
            .select_related("from_profile__user", "to_profile__user")
            .order_by("created")
        )
        total = candidates.count()
        if total == 0:
            self.stdout.write(f"No BLOCKED Friendship rows created before {cutoff_date.isoformat()}. Nothing to review.")
            return

        self.stdout.write(f"{total} BLOCKED Friendship row(s) created before {cutoff_date.isoformat()}:\n")
        flagged = 0
        for friendship in candidates:
            gap = friendship.updated - friendship.created
            reasons = []
            if gap > _FRESH_CREATE_TOLERANCE:
                reasons.append(f"updated {gap} after created")
            if friendship.request_message:
                reasons.append("has a request_message (started as a friend request, not a fresh block)")
            reused = bool(reasons)
            if reused:
                flagged += 1
            marker = self.style.WARNING("REVIEW    ") if reused else self.style.SUCCESS("likely fine")
            reason_text = f" - {'; '.join(reasons)}" if reasons else ""
            self.stdout.write(
                f"  [{marker}] Friendship #{friendship.pk}: "
                f"from_profile={friendship.from_profile_id} ({friendship.from_profile.user.username}) -> "
                f"to_profile={friendship.to_profile_id} ({friendship.to_profile.user.username}) "
                f"created={friendship.created.isoformat()} updated={friendship.updated.isoformat()}{reason_text}",
            )

        self.stdout.write("")
        self.stdout.write(
            f"{flagged} of {total} row(s) look reused from a pre-existing relationship and are worth a "
            f"manual look; {total - flagged} were most likely created directly as a block "
            "(Friendship.objects.create's always-correct path), even though they predate the fix.",
        )
        self.stdout.write("")
        self.stdout.write(
            "This command never writes. from_profile is recorded as the blocker "
            "(services.social.friendship._placed_the_block) - to correct a row confirmed to be "
            "inverted, swap from_profile_id and to_profile_id by hand once the people involved (or "
            "other records) confirm who actually placed the block.",
        )
