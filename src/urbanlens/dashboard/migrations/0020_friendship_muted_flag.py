"""Move "mute" off ``Friendship.status`` and onto its own boolean column.

Mute was implemented as a ``FriendshipStatus`` value, which forced one column
to carry two independent facts - *what kind of relationship is this* and *do I
want notifications from it* - and writing the second destroyed the first.
``Profile.are_friends`` matches ``status == "Accepted"`` and nothing else, so
every stored ``status="Muted"`` row is an accepted friendship that the app has
been treating as **not a friendship** for every visibility gate downstream:
profile fields, pin visibility, direct-message permission, friend-request
evaluation, and the common-pin/common-friend/common-trip queries. The same
conflation is why the profile page's Unmute button always answered 400 - it
posted to the friend-request endpoint, and ``FriendshipStatus.can_request``
excludes ``Muted``.

**Why the data fix restores ``Accepted``, and why that is faithful rather than
a guess.** The pre-mute status was never recorded, so it can only be recovered
by establishing which statuses mute was reachable *from*. There are three
code paths that have ever written ``Muted``:

1. ``FriendController.mute_friend`` -> ``services.friendship.mute_profile``.
   This is the only path a user can trigger, and the only template that
   renders its URL (``dashboard/partials/profile/_profile_hero_body.html``)
   emits the Mute button exclusively inside its ``friendship_status ==
   'accepted'`` branch. Every mute a real user has ever performed therefore
   started from ``Accepted``.
2. ``Friendship.mute()``, a classmethod that created a ``Muted`` row for two
   profiles with no relationship at all. ``git log -S`` over the whole history
   shows it was never called from any controller, service, or API module -
   only from its own unit test - so it has produced no production rows. It is
   deleted in this change.
3. The external API's ``FriendMuteView``, which calls the same service and so
   *could* mute a non-accepted row. It lives only on the unreleased
   ``feature/external-api-mobile-v2`` branch that introduces this migration,
   so no deployed database can contain a row it wrote.

Restoring to ``Accepted`` therefore returns each row to the exact state it
held before the mute, rather than inventing access: the pair *were* accepted
friends, and the app has merely been failing to treat them as such. Setting
them to something weaker (``Removed``/``Declined``) would look conservative
but would silently and permanently un-friend real friendships - it would make
the bug's effect permanent instead of repairing it.

**Reversibility.** The reverse re-encodes the flag into the old single-column
form: ``muted=True`` becomes ``status="Muted", muted=False``. That is exact
for any row this migration itself converted. It is *lossy* for rows muted
after the fact from a non-accepted status (only reachable via the external
API), because the old encoding simply cannot represent "declined and muted" -
which is the defect being fixed. The reverse is provided so the migration can
be rolled back at all; it is not a round-trip guarantee for post-migration
data.

The ``updated`` column is deliberately left alone by using a queryset
``.update()`` rather than per-row ``save()``: ``updated`` is ``auto_now`` and
the profile page renders it as the friendship's "since" date, so a repair pass
must not restamp every friendship's anniversary to the deploy date.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations, models

if TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

#: The legacy status value being retired. Spelled literally rather than
#: imported from ``FriendshipStatus`` - a data migration must keep working
#: even after the enum member is eventually deleted from the code.
_LEGACY_MUTED_STATUS = "Muted"

#: What a legacy muted row is restored to. See this module's docstring for why
#: this is a recovery of the prior state and not a widening of access.
_RESTORED_STATUS = "Accepted"


def restore_muted_friendships(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Convert legacy ``status="Muted"`` rows into accepted-but-muted rows.

    Args:
        apps: Historical app registry for this migration state.
        schema_editor: The active schema editor (unused; the fix is pure DML).
    """
    friendship_model = apps.get_model("dashboard", "Friendship")
    converted = friendship_model.objects.filter(status=_LEGACY_MUTED_STATUS).update(
        status=_RESTORED_STATUS,
        muted=True,
    )
    if converted:
        # Worth a record in the migrate output: this is the moment a set of
        # friendships stop being invisible to every visibility gate, which is
        # a user-observable behaviour change on deploy.
        print(f"  Restored {converted} muted friendship(s) to '{_RESTORED_STATUS}' with muted=True.")  # noqa: T201


def collapse_muted_flag_into_status(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Re-encode the ``muted`` flag back into the legacy status value.

    Lossy by construction for rows muted from a non-accepted status - the old
    single-column encoding has nowhere to keep the relationship state. See this
    module's docstring.

    Args:
        apps: Historical app registry for this migration state.
        schema_editor: The active schema editor (unused; the fix is pure DML).
    """
    friendship_model = apps.get_model("dashboard", "Friendship")
    friendship_model.objects.filter(muted=True).update(status=_LEGACY_MUTED_STATUS, muted=False)


class Migration(migrations.Migration):
    """Add ``Friendship.muted`` and repair the rows the old encoding corrupted."""

    dependencies = [
        ("dashboard", "0019_reaction_group_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="friendship",
            name="muted",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            restore_muted_friendships,
            collapse_muted_flag_into_status,
            elidable=False,
        ),
    ]
