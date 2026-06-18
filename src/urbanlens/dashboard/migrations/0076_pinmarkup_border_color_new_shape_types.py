from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0075_alter_notificationlog_notification_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinmarkup",
            name="border_color",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AlterField(
            model_name="pinmarkup",
            name="markup_type",
            field=models.CharField(
                choices=[
                    ("line", "Line"),
                    ("arrow", "Arrow"),
                    ("text", "Text"),
                    ("square", "Square"),
                    ("circle", "Circle"),
                    ("polygon", "Polygon"),
                ],
                max_length=20,
            ),
        ),
    ]
