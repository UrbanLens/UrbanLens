"""Add custom_icon column to dashboard_user_pins.

Pin.custom_icon was previously a django.forms.ImageField (not a model field),
so no DB column existed. This migration adds the proper column.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0013_tag_extended_merge_pinlist"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="custom_icon",
            field=models.ImageField(blank=True, null=True, upload_to="pin_custom_icons/"),
        ),
    ]
