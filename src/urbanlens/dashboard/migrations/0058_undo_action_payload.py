# Generated manually (see model docstring in models/undo/model.py for why
# cache_key was replaced with a durable payload column).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0057_pin_and_wiki_owners"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="undoaction",
            name="cache_key",
        ),
        migrations.AddField(
            model_name="undoaction",
            name="payload",
            field=models.JSONField(default=list),
            preserve_default=False,
        ),
    ]
