from django.db import migrations

def populate_categories(apps, schema_editor):
    Category = apps.get_model('dashboard', 'Category')
    categories = [
        {'name': 'Church', 'icon': 'church'},
        {'name': 'Warehouse', 'icon': 'warehouse'},
        {'name': 'Power Plant', 'icon': 'power'},
        # Add more categories here
    ]
    for category in categories:
        Category.objects.create(**category)

class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(populate_categories),
    ]
