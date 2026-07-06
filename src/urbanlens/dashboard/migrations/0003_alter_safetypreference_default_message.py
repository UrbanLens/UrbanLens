from django.db import migrations, models

import urbanlens.dashboard.models.safety.model


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0002_emergencycontactdefault_route_safetycheckin_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="safetypreference",
            name="default_message",
            field=models.TextField(blank=True, default=urbanlens.dashboard.models.safety.model.DEFAULT_CONTACT_MESSAGE),
        ),
    ]
