from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0040_alter_location_tags_alter_pin_tags"),
    ]

    operations = [
        migrations.CreateModel(
            name="TagCustomization",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(blank=True, max_length=255, null=True)),
                ("icon", models.CharField(blank=True, max_length=50, null=True)),
                ("color", models.CharField(blank=True, max_length=50, null=True)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tag_customizations",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "tag",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="customizations",
                        to="dashboard.tag",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_tag_customizations",
                "app_label": "dashboard",
            },
        ),
        migrations.AddConstraint(
            model_name="tagcustomization",
            constraint=models.UniqueConstraint(
                fields=["profile", "tag"],
                name="unique_tag_customization_per_profile",
            ),
        ),
    ]
