# Generated by Django 4.2.8 on 2024-01-08 05:16

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0004_alter_friendship_unique_together_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="profile",
            name="friends",
        ),
    ]
