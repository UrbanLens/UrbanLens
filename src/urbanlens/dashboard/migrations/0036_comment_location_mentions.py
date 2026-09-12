import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0035_api_call_log_attribution")]
    operations = [
        migrations.CreateModel(
            name="CommentLocationMention",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("location_uuid", models.UUIDField()),
                ("comment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="location_mentions", to="dashboard.comment")),
            ],
            options={
                "db_table": "dashboard_comment_location_mentions",
                "ordering": ["id"],
                "abstract": False,
                "indexes": [models.Index(fields=["location_uuid"], name="idxdb_cmtloc_uuid")],
                "constraints": [models.UniqueConstraint(fields=("comment", "location_uuid"), name="uq_cmtloc_one_per_comment")],
            },
        ),
    ]
