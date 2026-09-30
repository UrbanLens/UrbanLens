from django.db import migrations, models


def date_existing_blocks(apps, schema_editor):
    """Give every block already in place a time; its row's last update is the closest record of when it began."""
    Friendship = apps.get_model("dashboard", "Friendship")
    Friendship.objects.filter(status="Blocked", blocked_at__isnull=True).update(blocked_at=models.F("updated"))


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0116_pasted_images_are_linked_urls'),
    ]

    operations = [
        migrations.AddField(
            model_name='friendship',
            name='blocked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(date_existing_blocks, migrations.RunPython.noop),
    ]
