"""Replace album.manual_order with a live sort method; leave AlbumItem.order null until a drag."""

from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0033_album_cover_dedupe_map_hidden"),
    ]

    operations = [
        migrations.AddField(
            model_name="album",
            name="sort",
            field=models.CharField(
                choices=[
                    ("uploaded", "Date uploaded"),
                    ("taken", "Date taken"),
                    ("name", "Name"),
                    ("custom", "Custom"),
                ],
                default="uploaded",
                max_length=20,
            ),
        ),
        migrations.RemoveField(
            model_name="album",
            name="manual_order",
        ),
        migrations.AlterField(
            model_name="albumitem",
            name="order",
            field=models.IntegerField(blank=True, default=None, null=True),
        ),
    ]
