"""Give ``Reaction`` a fourth polymorphic host: group chat messages.

Reactions are stored in one table with a nullable foreign key per reactable
kind (comment, trip comment, direct message) and a *partial* unique constraint
per kind. Group messages were the one reactable surface missing from that set,
so the mobile client had no way to react in a group chat.

Operation order follows this package's convention (see ``migrations/CLAUDE.md``):
the column lands first, then the index, then the constraint - index/constraint
creation last, after every schema change they depend on.

The unique constraint is partial (``group_message IS NOT NULL``) for the same
reason its three siblings are: an unconditional unique index would also cover
every comment/trip-comment/direct-message reaction, whose ``group_message`` is
NULL and which can never collide anyway (PostgreSQL treats NULLs as distinct),
so the table would gain a fourth full-size index that only ever adjudicates a
quarter of its rows.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the ``group_message`` host column, its index, and its unique constraint."""

    dependencies = [
        ("dashboard", "0018_alter_friend_request_visibility_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="reaction",
            name="group_message",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reactions",
                to="dashboard.groupmessage",
            ),
        ),
        migrations.AddIndex(
            model_name="reaction",
            index=models.Index(fields=["group_message"], name="idxdb_react_gmsg"),
        ),
        migrations.AddConstraint(
            model_name="reaction",
            constraint=models.UniqueConstraint(
                condition=models.Q(("group_message__isnull", False)),
                fields=("profile", "emoji", "group_message"),
                name="unique_reaction_group_message",
            ),
        ),
    ]
