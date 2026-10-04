import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0039_location_cache_audience'),
    ]

    operations = [
        migrations.AddField(
            model_name='location',
            name='official_name_source',
            field=models.CharField(blank=True, default='', db_default='', max_length=50),
        ),
        migrations.CreateModel(
            name='LocationSlugHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('slug', models.SlugField(max_length=255, unique=True)),
                ('location', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slug_history', to='dashboard.location')),
            ],
            options={
                'verbose_name_plural': 'location slug history',
                'db_table': 'dashboard_location_slug_history',
                'abstract': False,
            },
        ),
    ]
