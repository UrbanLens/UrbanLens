

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0052_pg_stat_statements_extension'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='pin',
            index=models.Index(fields=['profile', 'id'], name='idxdb_pin_pfile_id'),
        ),
    ]
