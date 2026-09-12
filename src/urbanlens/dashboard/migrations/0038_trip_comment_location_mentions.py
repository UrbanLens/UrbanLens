from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0037_backfill_comment_location_mentions")]
    operations = [
        migrations.AddField(
            model_name="commentlocationmention",
            name="trip_comment",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="location_mentions", to="dashboard.tripcomment"),
        ),
        migrations.AlterField(
            model_name="commentlocationmention",
            name="comment",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="location_mentions", to="dashboard.comment"),
        ),
        migrations.AddConstraint(
            model_name="commentlocationmention",
            constraint=models.UniqueConstraint(fields=("trip_comment", "location_uuid"), name="uq_cmtloc_one_per_trip_comment"),
        ),
        migrations.AddConstraint(
            model_name="commentlocationmention",
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(("comment__isnull", False), ("trip_comment__isnull", True)), models.Q(("comment__isnull", True), ("trip_comment__isnull", False)), _connector="OR"),
                name="ck_cmtloc_exactly_one_owner",
            ),
        ),
    ]
