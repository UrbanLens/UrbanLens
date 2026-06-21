from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0099_alter_badge_kind_alter_friendinvitation_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationlog",
            name="source_profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="triggered_notifications",
                to="dashboard.profile",
            ),
        ),
    ]
