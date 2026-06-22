"""Add security_indicator to PinMarkup.

Lets users tag a drawn markup item (line, arrow, shape, etc.) as representing a
specific security feature (fence, camera, alarm, etc.).  When saved via the
controller the matching security field on the parent Pin is upgraded to at least
'some' if it is currently 'unknown' or 'no'.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0105_unprotect_user_kind_badges"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinmarkup",
            name="security_indicator",
            field=models.CharField(
                blank=True,
                choices=[
                    ("fence", "Fence"),
                    ("camera", "Camera"),
                    ("alarm", "Alarm"),
                    ("security", "Security Guard"),
                    ("sign", "Sign"),
                    ("plywood", "Plywood"),
                    ("locked", "Locked"),
                    ("vps", "VPS"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
