# Generated by Django 4.2.8 on 2023-12-31 23:51

from django.db import migrations
import djangofoundry.models.fields.char


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0003_alter_category_managers_alter_comment_managers_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="icon",
            field=djangofoundry.models.fields.char.CharField(
                blank=True, max_length=255, null=True
            ),
        ),
    ]