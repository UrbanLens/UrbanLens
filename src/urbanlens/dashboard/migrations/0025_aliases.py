"""Add PinAlias and LocationAlias models."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0024_backfill_pin_locations"),
    ]

    operations = [
        migrations.CreateModel(
            name="PinAlias",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                (
                    "pin",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="aliases",
                        to="dashboard.pin",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_pin_aliases",
                "ordering": ["name"],
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="pinalias",
            index=models.Index(fields=["pin"], name="dashboard_pin_alias_pin_idx"),
        ),
        migrations.AddConstraint(
            model_name="pinalias",
            constraint=models.UniqueConstraint(fields=["pin", "name"], name="dashboard_pin_alias_unique"),
        ),
        migrations.CreateModel(
            name="LocationAlias",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="aliases",
                        to="dashboard.location",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="location_aliases_created",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_location_aliases",
                "ordering": ["name"],
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="locationalias",
            index=models.Index(fields=["location"], name="dashboard_loc_alias_loc_idx"),
        ),
        migrations.AddConstraint(
            model_name="locationalias",
            constraint=models.UniqueConstraint(fields=["location", "name"], name="dashboard_loc_alias_unique"),
        ),
    ]
