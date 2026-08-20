"""Split ``Friendship.muted`` into one column per side of the relationship.

There is exactly one ``Friendship`` row per pair, so the single ``muted``
boolean was a property of the *relationship*: if A muted B, B's own view read
as muted too. Nothing consulted the flag, so nothing misbehaved - but wiring it
into notification delivery (which is the point of it existing) would have
silenced whichever of the two had not asked for silence.

Existing ``muted=True`` rows carry no record of who set them, and there is no
way to recover it. Both sides are set, which is what each of the two people
currently sees on the other's profile page - the Mute button reads the shared
flag, so both are already being shown "Muted" and offered "Unmute". Preserving
that view is the least surprising of the available answers, and either side can
now clear their own half.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0056_auto_tagging_setting"),
    ]

    operations = [
        migrations.AddField(
            model_name="friendship",
            name="muted_by_from_profile",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="friendship",
            name="muted_by_to_profile",
            field=models.BooleanField(default=False),
        ),
        migrations.RunSQL(
            sql="UPDATE dashboard_friendships SET muted_by_from_profile = TRUE, muted_by_to_profile = TRUE WHERE muted",
            reverse_sql="UPDATE dashboard_friendships SET muted = (muted_by_from_profile OR muted_by_to_profile)",
        ),
        migrations.RemoveField(
            model_name="friendship",
            name="muted",
        ),
    ]
