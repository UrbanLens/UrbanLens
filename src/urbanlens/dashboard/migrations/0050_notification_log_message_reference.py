from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0049_labels_name_upper_trgm_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationlog",
            name="direct_message",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="notifications",
                to="dashboard.directmessage",
            ),
        ),
        migrations.AddField(
            model_name="notificationlog",
            name="group_message",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="notifications",
                to="dashboard.groupmessage",
            ),
        ),
    ]
