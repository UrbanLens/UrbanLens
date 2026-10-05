"""Record each link's failed Wayback attempts and when its URL may be asked about again (P308)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0057_added_columns_database_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinlink",
            name="wayback_attempts",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="pinlink",
            name="wayback_retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="wikilink",
            name="wayback_attempts",
            field=models.PositiveSmallIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="wikilink",
            name="wayback_retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="pinlink",
            index=models.Index(condition=models.Q(("wayback_url", "")), fields=["wayback_retry_at"], name="db_plink_wayback_due"),
        ),
        migrations.AddIndex(
            model_name="wikilink",
            index=models.Index(condition=models.Q(("wayback_url", "")), fields=["wayback_retry_at"], name="db_wlink_wayback_due"),
        ),
    ]
