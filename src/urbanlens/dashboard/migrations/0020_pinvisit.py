from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0019_alter_tag_icon"),
    ]

    operations = [
        migrations.CreateModel(
            name="PinVisit",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("visited_at", models.DateTimeField()),
                ("notes", models.TextField(blank=True, null=True)),
                ("source", models.CharField(
                    choices=[("manual", "Manual"), ("google_takeout", "Google Takeout")],
                    default="manual",
                    max_length=20,
                )),
                ("pin", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="visit_history",
                    to="dashboard.pin",
                )),
            ],
            options={
                "db_table": "dashboard_pin_visits",
                "ordering": ["-visited_at"],
                "get_latest_by": "visited_at",
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="pinvisit",
            index=models.Index(fields=["pin"], name="dashboard_pv_pin_idx"),
        ),
        migrations.AddIndex(
            model_name="pinvisit",
            index=models.Index(fields=["pin", "visited_at"], name="dashboard_pv_pin_visited_idx"),
        ),
    ]
