from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0063_tripactivity_location_hidden"),
    ]

    operations = [
        migrations.CreateModel(
            name="TripActivityVote",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("vote", models.CharField(choices=[("up", "Up"), ("down", "Down")], max_length=4)),
                (
                    "activity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="votes",
                        to="dashboard.tripactivity",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activity_votes",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_trip_activity_votes",
                "unique_together": {("activity", "profile")},
            },
        ),
        migrations.AddIndex(
            model_name="tripactivityvote",
            index=models.Index(fields=["activity"], name="dashboard_tav_activity_idx"),
        ),
    ]
