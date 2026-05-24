from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0034_campus_multipolygon"),
    ]

    operations = [
        migrations.CreateModel(
            name="PinMarkup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                (
                    "markup_type",
                    models.CharField(
                        choices=[("line", "Line"), ("arrow", "Arrow"), ("text", "Text")],
                        max_length=20,
                    ),
                ),
                ("geometry", models.JSONField()),
                ("label", models.TextField(blank=True, default="")),
                ("color", models.CharField(blank=True, default="#e53e3e", max_length=20)),
                ("stroke_width", models.IntegerField(default=3)),
                (
                    "parent_pin",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="markup_items",
                        to="dashboard.pin",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="markup_items",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_pin_markup",
                "ordering": ["created"],
                "abstract": False,
                "indexes": [
                    models.Index(fields=["parent_pin"], name="dashboard_pm_pin_idx"),
                    models.Index(fields=["profile"], name="dashboard_pm_profile_idx"),
                ],
            },
        ),
    ]
