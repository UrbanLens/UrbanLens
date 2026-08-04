"""Drop what the Place backfill has finished with.

``BoundaryVote.location`` survived 0026 only so 0027 could map each vote onto
the place it was really about; 0027 deleted the votes it could not map, so the
column is now both redundant and empty of anything that matters, and
``place`` can take over as required.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0027_places_backfill"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="boundaryvote",
            name="location",
        ),
        migrations.AlterField(
            model_name="boundaryvote",
            name="place",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="boundary_votes",
                to="dashboard.place",
            ),
        ),
    ]
