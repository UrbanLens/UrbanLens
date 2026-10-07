"""Record when each alerted contact was told a check-in is over, so the notice goes to them once."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0071_safety_opt_out_email_normalized"),
    ]

    operations = [
        migrations.AddField(
            model_name="safetycheckincontact",
            name="resolution_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
