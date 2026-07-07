from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0003_profileemail_and_email_normalization"),
    ]

    operations = [
        migrations.AddField(
            model_name="safetycheckin",
            name="final_warning_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
