# Generated by Django 5.0.1 on 2024-03-22 20:02

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0012_alter_category_icon_trip"),
    ]

    operations = [
        migrations.AlterField(
            model_name="category",
            name="name",
            field=models.CharField(max_length=255, unique=True),
        ),
    ]
