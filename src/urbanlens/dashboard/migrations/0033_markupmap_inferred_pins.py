from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0032_markupmap_pin_markupmap_idxdb_mm_pin"),
    ]

    operations = [
        migrations.AddField(
            model_name="markupmap",
            name="inferred_pins",
            field=models.ManyToManyField(blank=True, related_name="inferred_maps", to="dashboard.pin"),
        ),
    ]
