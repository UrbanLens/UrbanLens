"""Record the rotation a markup map was drawn at, so a map turned to face its subject reopens that way."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0061_apicalllog_model_and_tokens"),
    ]

    operations = [
        migrations.AddField(
            model_name="markupmap",
            name="bearing",
            field=models.FloatField(db_default=0.0, default=0.0),
        ),
    ]
