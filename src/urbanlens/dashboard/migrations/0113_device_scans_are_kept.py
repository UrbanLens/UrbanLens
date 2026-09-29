import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0112_sitesettings_max_group_chats_per_user")]

    operations = [
        migrations.RemoveConstraint(model_name="sitesettings", name="device_scan_retention_days_gte_0"),
        migrations.RemoveField(model_name="sitesettings", name="device_scan_retention_days"),
        migrations.AlterField(
            model_name="devicescanupload",
            name="profile",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="device_scan_uploads", to="dashboard.profile"),
        ),
    ]
