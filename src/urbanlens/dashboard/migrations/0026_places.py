"""Schema for Place: one row per real-world parcel or building.

Split across three migrations so the data can move safely:

- **0026 (this one)** adds everything new, all nullable. Nothing reads it yet.
- **0027** backfills places from the existing per-location boundary copies,
  re-anchors wikis and votes, and grandfathers access nobody should lose.
- **0028** drops what the backfill has finished with.

``BoundaryVote.location`` is deliberately *not* dropped here - 0027 needs it to
map each vote onto the place it was really about.
"""

import django.contrib.gis.db.models.fields
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0025_redata_photo_relevance"),
    ]

    operations = [
        migrations.CreateModel(
            name="Place",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("parcel", "Parcel"),
                            ("building", "Building"),
                            ("site", "Site"),
                        ],
                        default="parcel",
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("current", "Current"), ("superseded", "Superseded")],
                        default="current",
                        max_length=20,
                    ),
                ),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "parent_relation",
                    models.CharField(
                        choices=[("part_of", "Part of"), ("member_of", "Member of")],
                        default="part_of",
                        max_length=20,
                    ),
                ),
                ("is_aggregate", models.BooleanField(default=False)),
                ("building_child_count", models.IntegerField(default=0)),
                (
                    "geometry",
                    django.contrib.gis.db.models.fields.MultiPolygonField(
                        blank=True, geography=True, null=True, srid=4326
                    ),
                ),
                ("area_sqm", models.FloatField(blank=True, null=True)),
                ("geometry_generated_at", models.DateTimeField(blank=True, null=True)),
                ("provider", models.CharField(blank=True, default="", max_length=20)),
                (
                    "provider_key",
                    models.CharField(blank=True, default="", max_length=255),
                ),
            ],
            options={
                "db_table": "dashboard_places",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="PlaceAccessGrant",
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
                    "reason",
                    models.CharField(
                        choices=[
                            ("backfill", "Held before places existed"),
                            ("split", "Held before the parcel was split"),
                        ],
                        max_length=20,
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_place_access_grants",
                "abstract": False,
            },
        ),
        migrations.RemoveConstraint(
            model_name="boundary",
            name="boundary_unique_location_default",
        ),
        migrations.RemoveConstraint(
            model_name="boundary",
            name="boundary_unique_source_candidate",
        ),
        migrations.RemoveConstraint(
            model_name="boundaryvote",
            name="db_boundary_vote_unique",
        ),
        migrations.RemoveIndex(
            model_name="boundaryvote",
            name="idxdb_bv_location",
        ),
        migrations.AddField(
            model_name="location",
            name="place_resolved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="place",
            name="domain_root",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="domain_members",
                to="dashboard.place",
            ),
        ),
        migrations.AddField(
            model_name="place",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="children",
                to="dashboard.place",
            ),
        ),
        migrations.AddField(
            model_name="boundary",
            name="place",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="boundary_candidates",
                to="dashboard.place",
            ),
        ),
        migrations.AddField(
            model_name="boundaryvote",
            name="place",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="boundary_votes",
                to="dashboard.place",
            ),
        ),
        migrations.AddField(
            model_name="location",
            name="place",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="locations",
                to="dashboard.place",
            ),
        ),
        migrations.AddField(
            model_name="wiki",
            name="place",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="wiki",
                to="dashboard.place",
            ),
        ),
        migrations.AddIndex(
            model_name="boundaryvote",
            index=models.Index(fields=["place"], name="idxdb_bv_place"),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(fields=["place"], name="idxdb_loc_place"),
        ),
        migrations.AddConstraint(
            model_name="boundary",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("pin__isnull", True),
                    ("place__isnull", False),
                    ("profile__isnull", True),
                    ("wiki__isnull", True),
                    models.Q(("source", ""), _negated=True),
                ),
                fields=("place", "boundary_type", "source"),
                name="boundary_unique_source_candidate",
            ),
        ),
        migrations.AddConstraint(
            model_name="boundaryvote",
            constraint=models.UniqueConstraint(
                fields=("place", "profile"), name="db_boundary_vote_unique"
            ),
        ),
        migrations.AddField(
            model_name="placeaccessgrant",
            name="place",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="access_grants",
                to="dashboard.place",
            ),
        ),
        migrations.AddField(
            model_name="placeaccessgrant",
            name="profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="place_access_grants",
                to="dashboard.profile",
            ),
        ),
        migrations.AddIndex(
            model_name="place",
            index=models.Index(fields=["domain_root"], name="idxdb_place_domain_root"),
        ),
        migrations.AddIndex(
            model_name="place",
            index=models.Index(fields=["parent"], name="idxdb_place_parent"),
        ),
        migrations.AddIndex(
            model_name="place",
            index=models.Index(
                fields=["status", "is_aggregate"], name="idxdb_place_resolvable"
            ),
        ),
        migrations.AddConstraint(
            model_name="place",
            constraint=models.UniqueConstraint(
                condition=models.Q(("provider_key", ""), _negated=True),
                fields=("provider", "provider_key", "kind"),
                name="place_unique_provider_record",
            ),
        ),
        migrations.AddIndex(
            model_name="placeaccessgrant",
            index=models.Index(fields=["profile"], name="idxdb_pag_profile"),
        ),
        migrations.AddConstraint(
            model_name="placeaccessgrant",
            constraint=models.UniqueConstraint(
                fields=("profile", "place"), name="place_access_grant_unique"
            ),
        ),
    ]
