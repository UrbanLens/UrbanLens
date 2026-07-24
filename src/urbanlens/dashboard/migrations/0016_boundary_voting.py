# Boundary voting: per-source candidate Boundary rows (Boundary.source) and
# the BoundaryVote model - recency-weighted community selection of which
# external provider's geometry is a location's official matching boundary.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0015_public_pin_voting"),
    ]

    operations = [
        migrations.AddField(
            model_name="boundary",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("redata", "County records (REData)"),
                    ("overpass", "OpenStreetMap (Overpass)"),
                ],
                default="",
                max_length=20,
            ),
        ),
        # The location-default uniqueness must now exclude source-candidate
        # rows, which share the "no pin/wiki/profile" shape but carry a source.
        migrations.RemoveConstraint(
            model_name="boundary",
            name="boundary_unique_location_default",
        ),
        migrations.AddConstraint(
            model_name="boundary",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("pin__isnull", True),
                    ("wiki__isnull", True),
                    ("profile__isnull", True),
                    ("source", ""),
                ),
                fields=("location", "boundary_type"),
                name="boundary_unique_location_default",
            ),
        ),
        migrations.AddConstraint(
            model_name="boundary",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    models.Q(
                        ("pin__isnull", True),
                        ("wiki__isnull", True),
                        ("profile__isnull", True),
                    ),
                    models.Q(("source", ""), _negated=True),
                ),
                fields=("location", "boundary_type", "source"),
                name="boundary_unique_source_candidate",
            ),
        ),
        migrations.CreateModel(
            name="BoundaryVote",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "boundary",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="votes",
                        to="dashboard.boundary",
                    ),
                ),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="boundary_votes",
                        to="dashboard.location",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="boundary_votes",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_boundary_votes",
                "abstract": False,
                "indexes": [models.Index(fields=["location"], name="idxdb_bv_location")],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("location", "profile"), name="db_boundary_vote_unique"
                    )
                ],
            },
        ),
    ]
