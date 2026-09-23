from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0053_pin_idxdb_pin_pfile_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='pin',
            name='auto_nested_buildings',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
