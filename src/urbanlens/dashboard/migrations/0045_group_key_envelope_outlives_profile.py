from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0044_upload_retries")]
    operations = [
        migrations.AlterField(
            model_name="groupkeyenvelope",
            name="profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="group_key_envelopes",
                to="dashboard.profile",
            ),
        ),
    ]
